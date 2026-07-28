"""
Claude Rolling Context Proxy

A transparent proxy between Claude Code and the Anthropic API.
Compresses old messages in the background using Haiku, keeping recent messages
verbatim. Zero latency — compression runs async, applied on the next request.

Uses content-based matching: hashes each message, recognizes previously compressed
messages by their content, and replaces them with the compressed version.
No sessions, no fingerprints — just content recognition.

Pure stdlib — no external dependencies needed.
"""

import collections
import datetime
import hashlib
import json
import os
import re
import sys
import logging
import threading
import time
import ssl
import socket
import http.client
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import chain
from compressor import RollingCompressor, SUMMARY_MARKER, NATIVE_MODE, SUMMARIZER_FORMAT, SUMMARIZER_MODEL

class FlushFileHandler(logging.FileHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()

_log_path = os.path.join(os.path.expanduser("~"), ".claude", "rolling-context-debug.log")
_log_handler = FlushFileHandler(_log_path, mode="a")
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), _log_handler],
)
log = logging.getLogger("rolling-context")

LISTEN_PORT = int(os.environ.get("ROLLING_CONTEXT_PORT") or "5588")


def _load_version() -> str:
    """Read the running proxy version from the canonical source.

    .claude-plugin/plugin.json (one dir up from proxy/) is the single version
    source of truth — the same file hooks/start-proxy.sh reads to detect a
    version change and restart the proxy. Resolve it relative to __file__ so
    it works regardless of the process cwd; never crash on a missing/malformed
    file — report "unknown" instead.
    """
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", ".claude-plugin", "plugin.json")
        with open(path, encoding="utf-8") as f:
            return (json.load(f) or {}).get("version") or "unknown"
    except Exception:
        return "unknown"


PROXY_VERSION = _load_version()


TRIGGER_TOKENS = int(os.environ.get("ROLLING_CONTEXT_TRIGGER") or "100000")
TARGET_TOKENS = int(os.environ.get("ROLLING_CONTEXT_TARGET") or "40000")
# Blend keep policy: keep between KEEP_FLOOR and KEEP_TURNS recent user-turns
# verbatim (whole turns), capped at TARGET tokens. Clamped in RollingCompressor.
KEEP_TURNS = int(os.environ.get("ROLLING_CONTEXT_KEEP_TURNS") or "8")
KEEP_FLOOR = int(os.environ.get("ROLLING_CONTEXT_KEEP_FLOOR") or "3")
# After a failed compression, wait this long before trying again — otherwise a
# failing summarizer (e.g. rate-limited) gets re-hammered on every request.
FAILURE_COOLDOWN = int(os.environ.get("ROLLING_CONTEXT_FAILURE_COOLDOWN") or "300")
# Cap on stored compression entries — without a bound, entries accumulate
# forever (removed only on "nothing to compress" / "no longer helps"),
# leaking memory and making find_match()'s scan O(entries) on every request.
STORE_MAX = int(os.environ.get("ROLLING_CONTEXT_STORE_MAX") or "32")
# entry["_debug_messages"] pins the whole compressed-away original message
# list for mismatch debugging. Off by default (unbounded retention would
# leak memory); when enabled, still capped to the most recent N messages.
DEBUG_MESSAGES_ENABLED = (os.environ.get("ROLLING_CONTEXT_DEBUG_MESSAGES") or "").strip().lower() in (
    "1", "true", "yes",
)
DEBUG_MESSAGES_CAP = 50

ssl_ctx = ssl.create_default_context()


def _join_path(upstream_path: str, request_path: str) -> str:
    """Join upstream path with request path, handling edge cases."""
    if not upstream_path:
        return request_path
    if not request_path or request_path == "/":
        return upstream_path
    if upstream_path.endswith("/") and request_path.startswith("/"):
        return upstream_path[:-1] + request_path
    if not upstream_path.endswith("/") and not request_path.startswith("/"):
        return upstream_path + "/" + request_path
    return upstream_path + request_path


compressor = RollingCompressor(
    trigger_tokens=TRIGGER_TOKENS,
    target_tokens=TARGET_TOKENS,
    summarizer_model=SUMMARIZER_MODEL,
    keep_turns=KEEP_TURNS,
    keep_floor=KEEP_FLOOR,
)


Upstream = collections.namedtuple("Upstream", ["scheme", "host", "port", "path", "source"])


class UpstreamRefused(Exception):
    """A chained upstream this daemon refuses to use: it either points back at
    ourselves (an infinite forwarding loop) or, for a file-sourced value
    (D18), is not a loopback address -- which would forward the API key
    off-machine to whatever wrote that file."""

    def __init__(self, url, path, reason):
        super().__init__(f"refusing to use chained upstream {url} from {path} ({reason})")
        self.url = url
        self.path = path
        self.reason = reason


_UPSTREAM_CACHE = {"stamp": None, "value": None}


def _settings_stamp():
    try:
        st = os.stat(chain.user_settings_path())
        return st.st_mtime_ns, st.st_size
    except FileNotFoundError:
        return None


def current_upstream() -> Upstream:
    """Resolve the upstream per request, cached on the settings file's mtime
    and size so a hot request path doesn't re-stat and re-parse JSON every
    call.

    Returns a parsed struct, not a string: a string-returning accessor only
    fixes the literal UPSTREAM_URL sites and leaves the connection factory
    building sockets from a frozen host/port -- that is the bug, not a
    symptom.

    Precedence (Fact 4, measured): the process environment beats settings
    files for ROLLING_CONTEXT_UPSTREAM -- the opposite of ANTHROPIC_BASE_URL's
    Fact 3 (chain.effective()), where files win. A file-sourced value must
    resolve to a loopback address (D18); an exported env var is the user
    acting deliberately in their own shell and is exempt from that check, but
    not from the self-loop check below -- pointing back at ourselves would
    forward every request to this same handler forever.
    """
    stamp = _settings_stamp()
    if _UPSTREAM_CACHE["value"] is not None and _UPSTREAM_CACHE["stamp"] == stamp:
        return _UPSTREAM_CACHE["value"]

    raw = os.environ.get("ROLLING_CONTEXT_UPSTREAM")
    from_env = bool(raw)
    if from_env and chain.is_self(raw):
        raise UpstreamRefused(raw, "<environment>", "loop")

    from_file = False
    if not raw:
        env = chain.read_settings(chain.user_settings_path()).get("env") or {}
        raw = env.get("ROLLING_CONTEXT_UPSTREAM")
        if raw:
            from_file = True
        else:
            candidate = env.get("ANTHROPIC_BASE_URL")
            if candidate and not chain.is_self(candidate):
                raw = candidate
                from_file = True

    if from_file and chain.is_self(raw):
        # Machine-written self-pointer: settings.json self-corrects on the
        # next chain/unchain, so treat this as "nothing configured" rather
        # than raising -- unlike a deliberate env export, this isn't a
        # decision anyone actually made.
        raw = None
        from_file = False

    raw = raw or "https://api.anthropic.com"

    if from_env:
        source = "<environment>"
    elif from_file:
        source = chain.user_settings_path()
    else:
        source = "(default)"

    # One typed failure out of this function. Callers already handle
    # UpstreamRefused; a bare ValueError escaping here killed the daemon at
    # startup and turned /health into a traceback.
    try:
        parsed = urlparse(raw)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UpstreamRefused(raw, source, "malformed") from exc

    # D18: a file-sourced upstream must be loopback, or a compromised or
    # careless writer of settings.json could redirect every request -- and
    # the API key with it -- off-machine. A malformed value parses to
    # hostname=None, which must be refused here rather than falling through
    # to a connection factory built from Upstream(host=None, ...).
    if from_file and not chain.host_matches(parsed.hostname, "127.0.0.1"):
        raise UpstreamRefused(raw, chain.user_settings_path(), "not-loopback")

    value = Upstream(
        parsed.scheme,
        parsed.hostname,
        port,
        parsed.path or "",
        source,
    )
    _UPSTREAM_CACHE.update(stamp=stamp, value=value)
    return value


def _upstream_conn(up: "Upstream"):
    """Create a connection to the upstream server."""
    if up.scheme == "https":
        return http.client.HTTPSConnection(
            up.host,
            up.port,
            context=ssl_ctx,
            timeout=600,
        )
    else:
        return http.client.HTTPConnection(
            up.host,
            up.port,
            timeout=600,
        )


def _upstream_reachable(up: "Upstream") -> bool:
    # ponytail: 0.5s probe timeout tuned for loopback chains; loosen if a
    # slow real-world WAN upstream makes /health flake.
    try:
        with socket.create_connection((up.host, up.port), timeout=0.5):
            return True
    except OSError:
        return False


def _api_error(message: str) -> bytes:
    return json.dumps({"type": "error", "error": {"type": "api_error", "message": message}}).encode()


def _upstream_error_body(exc: Exception) -> bytes:
    """D9: render upstream failures as a message Claude Code shows the user,
    not a raw transport exception."""
    resolved = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if isinstance(exc, UpstreamRefused):
        if exc.reason == "loop":
            return _api_error(
                f"rolling-context: refusing to use {exc.url} as upstream — it points back at "
                f"this proxy itself, which would forward every request in an infinite loop. "
                f"Fix ROLLING_CONTEXT_UPSTREAM in your shell.")
        if exc.reason == "malformed":
            return _api_error(
                f"rolling-context: refusing to use chained upstream {exc.url} from {exc.path} — "
                f"the value could not be parsed as a URL. "
                f"Fix or remove the value.")
        return _api_error(
            f"rolling-context: refusing to use chained upstream {exc.url} from {exc.path} — "
            f"it is not a loopback address, so forwarding would send your API key off-machine. "
            f"Fix or remove the value.")
    up = _UPSTREAM_CACHE.get("value")
    where = f"{up.scheme}://{up.host}:{up.port}" if up else "the chained upstream"
    if os.environ.get("ROLLING_CONTEXT_UPSTREAM"):
        return _api_error(
            f"rolling-context: chained upstream {where} is not answering. It comes from "
            f"ROLLING_CONTEXT_UPSTREAM in this session's environment, so unchain cannot clear it — "
            f"unset ROLLING_CONTEXT_UPSTREAM in your shell and restart the proxy.")
    return _api_error(
        f"rolling-context: chained upstream {where} is not answering. "
        f"Run: bash {resolved}/hooks/chain.sh unchain")


# ---------------------------------------------------------------------------
# Content-based matching
# ---------------------------------------------------------------------------

_VOLATILE_TAGS_RE = re.compile(
    r"<(?:system-reminder|local-command-caveat|local-command-stdout|"
    r"available-deferred-tools)>.*?</(?:system-reminder|local-command-caveat|"
    r"local-command-stdout|available-deferred-tools)>",
    re.DOTALL,
)


def _strip_volatile_tags(text: str) -> str:
    """Strip Claude Code's dynamic tags that change between requests."""
    return _VOLATILE_TAGS_RE.sub("", text)


def _normalize_content(content):
    """Strip volatile metadata (cache_control, system-reminder) for stable hashing."""
    if isinstance(content, str):
        return _strip_volatile_tags(content)
    if isinstance(content, list):
        result = []
        for block in content:
            if isinstance(block, dict):
                b = {}
                for k, v in block.items():
                    if k == "cache_control":
                        continue
                    if k == "content" and isinstance(v, (list, str)):
                        b[k] = _normalize_content(v)
                    elif k == "text" and isinstance(v, str):
                        b[k] = _strip_volatile_tags(v)
                    else:
                        b[k] = v
                result.append(b)
            else:
                result.append(block)
        return result
    return content


def _hash_message(msg: dict) -> str:
    """Stable hash of a message, ignoring cache_control metadata."""
    role = msg.get("role", "")
    content = _normalize_content(msg.get("content", ""))
    if not isinstance(content, str):
        content = json.dumps(content, sort_keys=True)
    raw = f"{role}:{content}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _hash_messages(messages: list) -> list:
    return [_hash_message(m) for m in messages]


class CompressionStore:
    """Content-based compression tracking. No sessions, no fingerprints, no keys.

    Stores a list of compressions. Each has original_hashes (what was compressed)
    and prefix (the replacement). On ANY request, scans messages — if the hashes
    match a stored compression, replaces them with the prefix.
    """

    def __init__(self, max_entries: int = None):
        self._lock = threading.Lock()
        self._compressions = []  # list of compression entries, oldest first
        self._max_entries = STORE_MAX if max_entries is None else max_entries

    def find_match(self, msg_hashes: list, messages: list = None):
        """Find a compression whose hash chain appears in msg_hashes.

        Returns the match whose chain ends furthest into the request
        (latest compression = covers the most history).
        Replaces everything up to and including the match, since the
        compression already contains a summary of everything before it.
        """
        with self._lock:
            best = None
            best_end = -1  # position in msg_hashes where the match ends
            for entry in self._compressions:
                oh = entry["original_hashes"]
                if not oh:
                    continue
                # Search for the hash chain in msg_hashes
                chain_len = len(oh)
                found = False
                for start in range(len(msg_hashes) - chain_len + 1):
                    if msg_hashes[start:start + chain_len] == oh:
                        end = start + chain_len
                        if end > best_end:
                            best = entry
                            best_end = end
                        found = True
                        break
                if not found and chain_len <= len(msg_hashes):
                    # Count total mismatches
                    mismatches = []
                    for i in range(min(chain_len, len(msg_hashes))):
                        if oh[i] != msg_hashes[i]:
                            mismatches.append(i)
                    log.warning(
                        f"[MATCH] No match: chain={chain_len} req={len(msg_hashes)} "
                        f"mismatches={len(mismatches)} at positions: "
                        f"{mismatches[:10]}{'...' if len(mismatches) > 10 else ''}"
                    )
                    # Dump content of first mismatched message for debugging
                    if mismatches and messages and entry.get("_debug_messages"):
                        idx = mismatches[0]
                        stored_msg = entry["_debug_messages"][idx] if idx < len(entry["_debug_messages"]) else None
                        incoming_msg = messages[idx] if idx < len(messages) else None
                        if stored_msg and incoming_msg:
                            s_content = str(stored_msg.get("content", ""))[:500]
                            i_content = str(incoming_msg.get("content", ""))[:500]
                            log.warning(
                                f"[MATCH] Mismatch at [{idx}] role={stored_msg.get('role')}:\n"
                                f"  STORED:   {s_content}\n"
                                f"  INCOMING: {i_content}"
                            )
            return best, best_end

    def _new_entry(self) -> dict:
        return {
            "original_hashes": [],   # hashes of original messages we replaced
            "prefix": None,          # compressed replacement messages
            "pending": None,         # pending compression result
            "pending_hashes": None,  # hashes for pending
            "thread": None,          # background compression thread
            "in_progress": False,    # reserved/running (set before thread start)
            "_debug_messages": None,  # optional mismatch-debug retention (see DEBUG_MESSAGES_ENABLED)
        }

    @staticmethod
    def _is_active(entry: dict) -> bool:
        """True if evicting this entry could pull the rug out from under a
        running (or about-to-run) background compression."""
        if entry.get("in_progress"):
            return True
        thread = entry.get("thread")
        return thread is not None and thread.is_alive()

    def _evict_locked(self):
        """Evict oldest non-active entries until at/under cap.

        Caller must hold self._lock. Never evicts an in-progress entry or one
        with a live compression thread — if every entry is active, the store
        stays over cap rather than dropping live state (correctness over the
        soft cap).
        """
        while len(self._compressions) > self._max_entries:
            idx = next(
                (i for i, e in enumerate(self._compressions) if not self._is_active(e)),
                None,
            )
            if idx is None:
                break
            del self._compressions[idx]

    def try_begin_compression(self):
        """Atomically reserve a compression slot.

        Under a single lock: refuse (return None) if any entry is already
        in-progress; otherwise create the entry, mark it in-progress BEFORE
        returning, and return it. Marking in_progress here — not after the
        thread's start() — is what closes the race: a just-reserved entry is
        visible to a concurrent caller even though its "thread" slot is still
        None. The caller starts the thread and assigns entry["thread"] after.
        """
        with self._lock:
            for e in self._compressions:
                if e.get("in_progress"):
                    return None
            entry = self._new_entry()
            entry["in_progress"] = True
            self._compressions.append(entry)
            self._evict_locked()  # entry is in_progress, so this can't evict it
            return entry

    def remove(self, entry: dict):
        with self._lock:
            self._compressions = [e for e in self._compressions if e is not entry]

    def promote_pending(self) -> int:
        """Atomically promote every ready pending compression to live.

        For each entry whose background write finished (pending is not None):
        move pending -> prefix and pending_hashes -> original_hashes, then
        clear both pending fields. The guard-read and the four field
        assignments run together under self._lock, so two concurrent request
        threads can't both pass the guard and race the transition — exactly one
        promotes each entry and the entry can never be corrupted to
        prefix=None/original_hashes=None (which find_match would drop, wasting
        the billed background compression). Composes with the M1 write-order in
        _do_background_compression (pending_hashes written before pending): the
        promoter reads+clears both fields atomically here. Pure in-memory work,
        no blocking call, so holding the lock is safe.

        Returns the number of entries promoted (for logging/tests).
        """
        promoted = []
        with self._lock:
            for entry in self._compressions:
                if entry["pending"] is not None:
                    entry["prefix"] = entry["pending"]
                    entry["original_hashes"] = entry["pending_hashes"]
                    entry["pending"] = None
                    entry["pending_hashes"] = None
                    promoted.append(entry)
        for entry in promoted:
            log.info(
                f"[MSG] Compression promoted: {len(entry['prefix'])} prefix messages "
                f"replacing {len(entry['original_hashes'])} originals"
            )
        return len(promoted)

    @property
    def compressions(self):
        # Return a snapshot under the lock so readers (promote loop, health,
        # debug) iterate a stable list even while other threads add/remove.
        with self._lock:
            return list(self._compressions)


store = CompressionStore()


# Ring buffer of the last few requests routed through the proxy, for /health
# observability: incoming vs forwarded context size per request. Bounded by
# maxlen so it can never grow; lock-guarded on both write and read because
# ThreadedHTTPServer runs each request on its own thread (mirrors the
# CompressionStore lock discipline).
_recent_requests = collections.deque(maxlen=3)
_recent_lock = threading.Lock()


def _record_request(before_chars, after_chars, injected, after_tokens):
    """Append one routed-request record (newest last) under the lock."""
    record = {
        "ts": time.time(),
        "before_chars": before_chars,
        "after_chars": after_chars,
        "injected": injected,
        "after_tokens": after_tokens,
    }
    with _recent_lock:
        _recent_requests.append(record)


def _recent_snapshot():
    """Stable list snapshot (oldest first) under the lock."""
    with _recent_lock:
        return list(_recent_requests)


def _iso(ts):
    """Epoch seconds -> ISO 8601 local datetime string (e.g. 2026-07-25T16:30:00+02:00)."""
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _forward_headers(req_headers: dict, body: bytes = None, strip_encoding: bool = False) -> dict:
    """Build headers for an outbound proxy request (raw pass-through and the
    /v1/messages forward). NOT interchangeable with get_passthrough_headers:
    this also strips "connection", recomputes "content-length" for a
    (possibly rewritten) body, and can drop "accept-encoding" to force plain
    SSE — none of which apply to the internal auth_headers snapshot that
    get_passthrough_headers produces for later reuse by the compressor."""
    headers = {}
    for key, value in req_headers.items():
        lower = key.lower()
        if lower in ("host", "transfer-encoding", "connection", "content-length"):
            continue
        if strip_encoding and lower == "accept-encoding":
            continue
        headers[key] = value
    if body is not None:
        headers["content-length"] = str(len(body))
    our_host, our_port = chain.our_bind()
    headers["X-Rolling-Context-Chained-From"] = f"http://{our_host}:{our_port}"
    log.debug(f"[HDR] Forwarding headers: {list(headers.keys())}")
    return headers


def _loop_detected(req_headers: dict) -> bool:
    """True when an inbound request already carries our own chained-from
    marker (spec section 7): a cycle through an intermediate proxy, which
    is_self alone (a direct self-chain check) would not catch. Uses the
    same host_matches normalization is_self does -- a raw string compare
    misses localhost against 127.0.0.1."""
    marker = None
    for key, value in req_headers.items():
        if key.lower() == "x-rolling-context-chained-from":
            marker = value
            break
    if not marker:
        return False
    try:
        parsed = urlparse(marker)
    except ValueError:
        return False
    if not parsed.hostname or not parsed.port:
        return False
    our_host, our_port = chain.our_bind()
    return chain.host_matches(parsed.hostname, our_host) and parsed.port == our_port


def get_passthrough_headers(req_headers: dict) -> dict:
    # Not a duplicate of _forward_headers: this keeps "connection" and never
    # overrides "content-length" (see _forward_headers' docstring).
    headers = {}
    for key, value in req_headers.items():
        lower = key.lower()
        if lower not in ("host", "content-length", "transfer-encoding"):
            headers[key] = value
    return headers


def _validate_tool_pairs(messages: list) -> list:
    """Drop leading orphaned tool_result references (their tool_use was cut
    away by compression) without disturbing anything else.

    The old implementation scanned the WHOLE array for any orphaned
    tool_result and cut everything up to the last one it found — which could
    slice through the injected [summary, ack] prefix (breaking the
    SUMMARY_MARKER@[0]/ack@[1] contract) or discard valid, unrelated pairs
    deeper in the conversation. This version only ever trims a leading run:
    a message consisting entirely of orphaned tool_result blocks is dropped
    outright, along with (if present) the dangling reply that followed it; a
    leading message that MIXES an orphaned tool_result with other blocks
    (e.g. text) has only the orphaned block(s) stripped, keeping the rest of
    the message in place. Either way this stops at the first surviving,
    non-orphan message so the result stays user-first.
    """
    if not messages:
        return messages

    # The injected compression prefix is exactly [summary(user), ack(assistant)]
    # (see _do_background_compression) and never carries tool blocks — never
    # trim into it.
    prefix_len = 0
    content0 = messages[0].get("content", "")
    if (messages[0].get("role") == "user"
            and isinstance(content0, str) and SUMMARY_MARKER in content0
            and len(messages) > 1 and messages[1].get("role") == "assistant"):
        prefix_len = 2

    body = list(messages[prefix_len:])
    if not body:
        return messages

    known_ids = set()
    for msg in body:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    known_ids.add(block.get("id", ""))

    def _is_orphan_tool_result(msg):
        """Indices of `msg`'s tool_result blocks whose tool_use_id has no
        matching tool_use in `known_ids` — empty if msg has none. A message
        can mix an orphaned tool_result with unrelated blocks (e.g. text),
        so this reports which blocks are orphaned rather than an all-or-
        nothing verdict on the whole message."""
        content = msg.get("content", "")
        if not isinstance(content, list):
            return []
        return [
            i for i, b in enumerate(content)
            if isinstance(b, dict) and b.get("type") == "tool_result"
            and b.get("tool_use_id", "") not in known_ids
        ]

    drop = 0
    while drop < len(body):
        orphan_idx = _is_orphan_tool_result(body[drop])
        if not orphan_idx:
            break  # clean, non-orphan message — stop trimming here

        content = body[drop]["content"]
        survivors = [b for i, b in enumerate(content) if i not in orphan_idx]
        if survivors:
            # Mixed content: strip only the orphaned tool_result block(s)
            # and keep the message's other blocks in place. The message
            # still opens with (or contains) live content, so alternation
            # and the user-first guarantee both hold without dropping
            # anything further.
            body[drop] = {**body[drop], "content": survivors}
            break

        # The whole message was nothing but orphaned tool_result blocks —
        # drop it and its now-dangling reply, then re-check what follows.
        drop += 1
        while drop < len(body) and body[drop].get("role") != "user":
            drop += 1

    if drop:
        log.info(f"Dropping {drop} leading messages with orphaned tool_result references")

    return messages[:prefix_len] + body[drop:]


_compression_failed_at = 0.0

# Wall-clock time of the last compression injection — the moment old messages
# actually left the model's context. Exposed at /lean/status so companion
# plugins (nestor-lean) can invalidate "the model already saw this" knowledge.
_last_injection_ts = 0.0


def _do_background_compression(entry: dict, messages: list, auth_headers: dict,
                               real_token_count: int = None, payload: dict = None):
    """Compress messages. Key = hashes of messages that were summarized (not kept verbatim)."""
    global _compression_failed_at
    log.info(f"[BG] Starting compression of {len(messages)} messages...")
    try:
        compressed = compressor.compress(messages, auth_headers,
                                         real_token_count=real_token_count, payload=payload)
        if compressed is None:
            # Nothing worth compressing — don't leave an empty entry behind
            store.remove(entry)
            return
        # compressed = [summary, ack] + recent_verbatim
        # Prefix = ONLY [summary, ack] — verbatim messages come from the
        # original request during injection, so including them in the prefix
        # would cause duplication.
        prefix = compressed[:2]
        # Key = the messages that were summarized away (not the verbatim ones).
        recent_count = len(compressed) - 2  # subtract summary + ack
        summarized = messages[:len(messages) - recent_count]
        # Skip old summary prefix if present
        start = 0
        if summarized and isinstance(summarized[0].get("content", ""), str):
            if SUMMARY_MARKER in summarized[0]["content"]:
                start = 2
        key_hashes = _hash_messages(summarized[start:])
        # Publish ordering: the promote loop (a different, unlocked request
        # thread) guards on "pending" and then reads "pending_hashes", so write
        # pending_hashes FIRST and the guard field "pending" LAST. Otherwise an
        # interleave could promote pending with pending_hashes still None, which
        # sets original_hashes=None and the compression never matches (wasted).
        entry["pending_hashes"] = key_hashes
        entry["pending"] = prefix
        if DEBUG_MESSAGES_ENABLED:
            # For mismatch debugging only — capped so an opted-in debug run
            # still can't pin an unbounded amount of message history.
            entry["_debug_messages"] = summarized[start:][-DEBUG_MESSAGES_CAP:]
        log.info(
            f"[BG] Compression ready: "
            f"{compressor._count_chars(prefix):,} chars "
            f"({len(prefix)} prefix messages, key={len(key_hashes)} hashes, "
            f"summarized {len(summarized) - start} messages)"
        )
    except Exception as e:
        _compression_failed_at = time.time()
        log.error(
            f"[BG] Compression failed (cooling down {FAILURE_COOLDOWN}s): {e}",
            exc_info=True,
        )
        entry["pending"] = None
    finally:
        # Release the reservation once work is done (success, failure, or the
        # "nothing to compress" early return) so a later request can compress
        # again. Cleared last so no duplicate can slip in while we were running.
        entry["in_progress"] = False


class ProxyHandler(BaseHTTPRequestHandler):
    """Handle HTTP requests, proxy to upstream API."""
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("content-length", 0))
        return self.rfile.read(length) if length > 0 else b""

    def _get_headers_dict(self) -> dict:
        return {key: value for key, value in self.headers.items()}

    def _proxy_raw(self, method: str):
        """Raw proxy — forward request and stream response back."""
        body = self._read_body()
        req_headers = self._get_headers_dict()
        if _loop_detected(req_headers):
            error_body = _api_error(
                "rolling-context: refusing to forward — this request already passed "
                "through this proxy once (loop).")
            self.send_response(200)  # D9: message body, not a transport-level error status
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return
        headers = _forward_headers(req_headers, body if body else None)

        try:
            up = current_upstream()
            log.info(f"[RAW] {method} {self.path} -> {up.scheme}://{up.host}:{up.port} (body={len(body)} bytes)")
            conn = _upstream_conn(up)
            upstream_full_path = _join_path(up.path, self.path)
            conn.request(method, upstream_full_path, body=body if body else None, headers=headers)
            resp = conn.getresponse()

            log.info(f"[RAW] Response: {resp.status} {resp.reason}")

            self.send_response(resp.status)
            resp_headers = resp.getheaders()
            log.debug(f"[RAW] Response header names: {[k for k, _ in resp_headers]}")
            has_content_length = False
            for key, value in resp_headers:
                lower = key.lower()
                if lower in ("connection", "transfer-encoding"):
                    continue
                if lower == "content-length":
                    has_content_length = True
                self.send_header(key, value)
            if not has_content_length:
                self.send_header("Connection", "close")
            self.end_headers()

            total_bytes = 0
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                total_bytes += len(chunk)

            log.info(f"[RAW] Done streaming {total_bytes:,} bytes")
            conn.close()
        except Exception as e:
            log.error(f"[RAW] Upstream error: {e}", exc_info=True)
            error_body = _upstream_error_body(e)
            self.send_response(200)  # D9: message body, not a transport-level error status
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)

    def do_GET(self):
        log.info(f"[REQ] GET {self.path}")
        parsed = urlparse(self.path)
        normalized_path = parsed.path
        if normalized_path == "/health":
            self._handle_health()
        elif normalized_path == "/debug/compressions":
            self._handle_debug_compressions()
        elif normalized_path == "/lean/status":
            self._handle_lean_status()
        else:
            self._proxy_raw("GET")

    def do_POST(self):
        log.info(f"[REQ] POST {self.path}")
        if self.path.startswith("/v1/messages"):
            self._handle_messages()
        else:
            self._proxy_raw("POST")

    def do_PUT(self):
        log.info(f"[REQ] PUT {self.path}")
        self._proxy_raw("PUT")

    def do_DELETE(self):
        log.info(f"[REQ] DELETE {self.path}")
        self._proxy_raw("DELETE")

    def do_PATCH(self):
        log.info(f"[REQ] PATCH {self.path}")
        self._proxy_raw("PATCH")

    def do_OPTIONS(self):
        log.info(f"[REQ] OPTIONS {self.path}")
        self._proxy_raw("OPTIONS")

    def _handle_debug_compressions(self):
        entries = []
        for i, entry in enumerate(store.compressions):
            info = {
                "index": i,
                "hash_chain_length": len(entry.get("original_hashes") or []),
                "has_prefix": entry["prefix"] is not None,
                "prefix_content": None,
            }
            if entry["prefix"]:
                for msg in entry["prefix"]:
                    content = msg.get("content", "")
                    if isinstance(content, str) and SUMMARY_MARKER in content:
                        info["prefix_content"] = content
            entries.append(info)
        body = json.dumps(entries, indent=2).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_lean_status(self):
        """Machine-readable status for companion plugins (nestor-lean).

        last_injection_ts is global across all conversations flowing through
        this proxy — consumers must treat it as a conservative signal (a
        compression in ANY session invalidates, which only costs savings,
        never correctness).
        """
        data = {
            "status": "ok",
            "last_injection_ts": _last_injection_ts,
            "stored_compressions": len(store.compressions),
        }
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_health(self):
        active = sum(
            1 for e in store.compressions
            if e["thread"] is not None and e["thread"].is_alive()
        )
        try:
            up = current_upstream()
            upstream_url = f"{up.scheme}://{up.host}:{up.port}"
            upstream_source = chain.display(up.source)
            chained = up.host != "api.anthropic.com"
            upstream_reachable = _upstream_reachable(up)
        except UpstreamRefused as e:
            upstream_url = f"(refused: {e.reason})"
            upstream_source = chain.display(e.path)
            chained = True
            upstream_reachable = False
        except chain.UnparseableSettings as e:
            # /health is the visibility surface for exactly this failure: a
            # hand-broken settings.json must read here as a diagnostic naming
            # the file, never as a traceback out of do_GET.
            upstream_url = "(unresolved: settings file is not valid JSON)"
            upstream_source = chain.display(e.path)
            chained = False
            upstream_reachable = False
        data = {
            "status": "ok",
            "version": PROXY_VERSION,
            "trigger_tokens": TRIGGER_TOKENS,
            "target_tokens": TARGET_TOKENS,
            "keep_turns": compressor.keep_turns,
            "keep_floor": compressor.keep_floor,
            "summarizer_model": SUMMARIZER_MODEL or "(session model)",
            "summarizer_mode": "native" if NATIVE_MODE else f"flattened/{SUMMARIZER_FORMAT}",
            "upstream_url": upstream_url,
            "upstream_source": upstream_source,
            "chained": chained,
            "upstream_reachable": upstream_reachable,
            "compression_count": compressor.compression_count,
            "total_tokens_saved": compressor.total_tokens_saved,
            "stored_compressions": len(store.compressions),
            "active_compressions": active,
            "recent_requests": [
                {**r, "ts": _iso(r["ts"])} for r in reversed(_recent_snapshot())
            ],
            "last_compression": (
                {**compressor.last_compression,
                 "ts": _iso(compressor.last_compression["ts"])}
                if compressor.last_compression else None
            ),
        }
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_messages(self):
        raw_body = self._read_body()
        req_headers = self._get_headers_dict()
        if _loop_detected(req_headers):
            error_body = _api_error(
                "rolling-context: refusing to forward — this request already passed "
                "through this proxy once (loop).")
            self.send_response(200)  # D9: message body, not a transport-level error status
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return
        auth_headers = get_passthrough_headers(req_headers)

        log.info(f"[MSG] POST {self.path} (body={len(raw_body)} bytes)")
        log.debug(f"[MSG] Request headers: {list(req_headers.keys())}")

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            log.error("[MSG] Invalid JSON in request body")
            error_body = b'{"error":"Invalid JSON"}'
            self.send_response(400)
            self.send_header("content-length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return

        messages = payload.get("messages", [])
        is_streaming = payload.get("stream", False)
        model = payload.get("model", "unknown")

        # Hash all messages for content-based matching
        msg_hashes = _hash_messages(messages)
        msg_chars = compressor._count_chars(messages)
        incoming_chars = msg_chars  # "before" size, captured pre-injection

        log.info(
            f"[MSG] model={model} stream={is_streaming} "
            f"messages={len(messages)} chars={msg_chars:,}"
        )

        # Promote any pending compressions (atomic per-entry under the store
        # lock — see CompressionStore.promote_pending).
        store.promote_pending()

        # Scan: do any stored compressions match this request's messages?
        match, match_end = store.find_match(msg_hashes, messages)
        injected = False

        if match and match["prefix"] is not None and match_end > 0:
            # Replace everything up to match_end with the prefix
            # (prefix contains summary of everything before it)
            new_messages = messages[match_end:]

            merged = match["prefix"] + new_messages
            merged = _validate_tool_pairs(merged)

            merged_chars = compressor._count_chars(merged)
            if merged_chars < msg_chars:
                log.info(
                    f"[MSG] Injecting: {msg_chars:,} -> {merged_chars:,} chars "
                    f"({len(messages)} -> {len(merged)} messages, "
                    f"replaced 0-{match_end} with {len(match['prefix'])} prefix "
                    f"+ {len(new_messages)} new)"
                )
                payload["messages"] = merged
                msg_chars = merged_chars
                injected = True
                global _last_injection_ts
                _last_injection_ts = time.time()
            else:
                log.info(
                    f"[MSG] Compression no longer helps: "
                    f"merged={merged_chars:,} >= current={msg_chars:,} chars, removing"
                )
                store.remove(match)
                match = None

        # Save current state for post-response compression trigger
        current_messages = payload.get("messages", messages)

        # Forward request — strip Accept-Encoding so we get plain text SSE
        body = json.dumps(payload).encode()
        headers = _forward_headers(req_headers, body, strip_encoding=True)

        try:
            up = current_upstream()
            log.info(f"[MSG] Forwarding to {up.scheme}://{up.host}:{up.port}{self.path} ({len(body):,} bytes)")
            conn = _upstream_conn(up)
            upstream_full_path = _join_path(up.path, self.path)
            conn.request("POST", upstream_full_path, body=body, headers=headers)
            resp = conn.getresponse()

            log.info(f"[MSG] Upstream response: {resp.status} {resp.reason}")

            self.send_response(resp.status)
            resp_headers = resp.getheaders()
            log.debug(f"[MSG] Response header names: {[k for k, _ in resp_headers]}")
            has_content_length = False
            for key, value in resp_headers:
                lower = key.lower()
                if lower in ("connection", "transfer-encoding"):
                    continue
                if lower == "content-length":
                    has_content_length = True
                self.send_header(key, value)
            if not has_content_length:
                self.send_header("Connection", "close")
            self.end_headers()

            log.info(f"[MSG] Streaming response...")

            # Stream response and capture SSE token data. bytearray so the
            # per-chunk accumulation is an in-place O(n) extend, not the O(n^2)
            # realloc-and-copy of `bytes += bytes` (runs for every response now
            # that non-streaming bodies are buffered too).
            buffer = bytearray()
            total_bytes = 0
            total_input = 0
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                total_bytes += len(chunk)
                buffer += chunk

            log.info(f"[MSG] Done streaming {total_bytes:,} bytes")

            # Extract input tokens from SSE stream
            if is_streaming and buffer:
                try:
                    text = buffer.decode("utf-8", errors="replace")
                    lines = text.split("\n")
                    sse_event_count = 0
                    for line in lines:
                        if not line.startswith("data: "):
                            continue
                        sse_event_count += 1
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            continue
                        # Cheap pre-check: skip the (usually vast majority of)
                        # content_block_delta/ping/etc. lines without paying
                        # for a json.loads on each one.
                        if "message_start" not in data_str and "message_delta" not in data_str:
                            continue
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        evt_type = data.get("type", "")

                        # Anthropic native: usage in message_start.message.usage
                        if evt_type == "message_start":
                            usage = data.get("message", {}).get("usage", {})
                            tokens = (
                                usage.get("input_tokens", 0)
                                + usage.get("cache_creation_input_tokens", 0)
                                + usage.get("cache_read_input_tokens", 0)
                            )
                            if tokens > 0:
                                total_input = tokens
                                log.info(f"[MSG] Input tokens from message_start: {total_input:,}")

                        # Proxy/converter: usage in message_delta.usage (e.g. CodeGate)
                        elif evt_type == "message_delta":
                            usage = data.get("usage", {})
                            tokens = int(usage.get("input_tokens", 0))
                            if tokens > 0 and tokens > total_input:
                                total_input = tokens
                                log.info(f"[MSG] Input tokens from message_delta: {total_input:,}")
                            # message_delta is the last usage-bearing event in
                            # the stream — nothing after it can change total_input.
                            break

                    if total_input == 0:
                        log.warning(
                            f"[MSG] No input tokens found in SSE! "
                            f"Total events: {sse_event_count}"
                        )
                except Exception as e:
                    log.warning(f"[MSG] Failed to parse SSE for tokens: {e}")
            elif not is_streaming and buffer:
                try:
                    data = json.loads(buffer)
                    usage = data.get("usage", {})
                    total_input = (
                        usage.get("input_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                    )
                    if total_input > 0:
                        log.info(f"[MSG] Input tokens from response: {total_input:,}")
                except Exception as e:
                    log.warning(f"[MSG] Failed to parse response for tokens: {e}")

            conn.close()

            # Snapshot the REAL upstream token count BEFORE the char fallback
            # below can overwrite total_input with an estimate — /health must
            # report real tokens only (0 when upstream reported none).
            real_after_tokens = total_input

            # Fallback: estimate tokens from chars if SSE didn't provide usage
            if total_input == 0 and msg_chars > 0:
                total_input = msg_chars // 4  # rough chars-to-tokens estimate
                log.info(
                    f"[MSG] No tokens from SSE, estimating from chars: "
                    f"{msg_chars:,} chars -> ~{total_input:,} tokens"
                )

            # Record this request for /health observability: incoming vs
            # forwarded ("after") context size. after_tokens is the real
            # upstream count captured above, never the char estimate.
            _record_request(incoming_chars, msg_chars, injected, real_after_tokens)

            # Trigger compression based on token count. The minimum message
            # count keeps us from "compressing" sessions whose bulk is the
            # system prompt / first-message context, which we can't remove.
            if total_input > 0 and total_input > TRIGGER_TOKENS and len(current_messages) >= 6:
                cooldown_left = FAILURE_COOLDOWN - (time.time() - _compression_failed_at)
                if cooldown_left > 0:
                    log.info(
                        f"[MSG] Over trigger but last compression failed — "
                        f"cooling down another {cooldown_left:.0f}s"
                    )
                else:
                    # Atomic check-add-reserve: returns None if a compression is
                    # already in progress, so two concurrent over-trigger requests
                    # cannot both spawn one (each spawn = a real upstream call).
                    entry = store.try_begin_compression()
                    if entry is None:
                        pass  # already compressing
                    else:
                        log.info(
                            f"[MSG] API reported {total_input:,} tokens (trigger: {TRIGGER_TOKENS:,}). "
                            f"Compressing in background..."
                        )
                        t = threading.Thread(
                            target=_do_background_compression,
                            args=(entry, current_messages, auth_headers),
                            kwargs={"real_token_count": total_input, "payload": payload},
                            daemon=True,
                        )
                        # entry is already reserved (in_progress=True); assigning
                        # the thread here just enriches diagnostics.
                        t.start()
                        entry["thread"] = t

        except Exception as e:
            log.error(f"[MSG] Upstream error: {e}", exc_info=True)
            error_body = _upstream_error_body(e)
            self.send_response(200)  # D9: message body, not a transport-level error status
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)


class ThreadedHTTPServer(HTTPServer):
    """Handle each request in a new thread."""
    def process_request(self, request, client_address):
        t = threading.Thread(target=self._handle, args=(request, client_address))
        t.daemon = True
        t.start()

    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


def main():
    log.info(f"Starting Rolling Context Proxy on port {LISTEN_PORT}")
    log.info(f"  Trigger at: {TRIGGER_TOKENS:,} tokens")
    log.info(f"  Compress down to: {TARGET_TOKENS:,} tokens (recent context)")
    log.info(f"  Keep recent turns: {compressor.keep_floor}..{compressor.keep_turns} user-turns")
    log.info(f"  Summarizer model: {SUMMARIZER_MODEL or '(session model)'}")
    log.info(f"  Summarizer mode: "
             f"{'native (cloned session request, prompt-cached)' if NATIVE_MODE else f'flattened/{SUMMARIZER_FORMAT}'}")
    try:
        _startup_upstream = current_upstream()
        log.info(f"  Forwarding to: {_startup_upstream.scheme}://{_startup_upstream.host}:{_startup_upstream.port}")
    except UpstreamRefused as e:
        log.warning(f"  Upstream refused at startup: {e}")
    except chain.UnparseableSettings as e:
        # Startup must degrade the way /health does: a hand-broken settings.json
        # is a diagnostic, never a reason for the daemon not to exist. The
        # upstream is re-resolved per request, so a later fix needs no restart.
        log.warning(
            f"  Upstream unresolved at startup: {chain.display(e.path)} is not valid JSON"
            f" -- serving anyway")
    log.info(f"  Matching: content-based (no sessions/fingerprints)")

    server = ThreadedHTTPServer(("127.0.0.1", LISTEN_PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
