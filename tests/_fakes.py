"""Shared test doubles for tests/test_*.py.

Deduplicates the copy-pasted `_FakeResponse`/`_FakeConn` (summarizer-facing)
and `FakeResponse`/`FakeConn`/`_make_handler` (ProxyHandler-facing) doubles
that were previously pasted verbatim across multiple test files. Each
call site's exact captured/returned bytes and assertions are unchanged --
these are parameterized to cover the observed variants (fixed vs. custom
reply text, request-body capture on/off).

tests/test_flattened_reply_guard.py keeps its own JSON-reply conn local (its
response shape is JSON, not SSE) but imports FakeResponse from here.

Gemini-e86.18. Also carries e86.16's `seed_evictable(store)` store helper
(appended below), which replicates the deleted CompressionStore.add()'s
append+evict primitive for the eviction/cap tests.

Run: python3 -m unittest discover -s tests
"""
import io
import json
import os
import sys
import tempfile
import threading
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
from server import ProxyHandler  # noqa: E402


class FakeResponse:
    """Minimal http.client response stand-in for the summarizer connection:
    a status plus a body readable once via .read()."""

    def __init__(self, body: bytes, status: int = 200):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class FakeSummarizerConn:
    """Fake `_summarizer_conn()` return value. By default every
    getresponse() emits a content_block_delta carrying `reply_text` plus a
    message_delta carrying `stop_reason` (None => no message_delta). Pass
    `replies=[{"text":..., "stop_reason":...}, ...]` to return different
    bodies on successive getresponse() calls (consumed in order; the last
    entry repeats once exhausted). `capture` records outgoing JSON bodies
    on `.bodies` (list) and `.last_body` (most recent)."""

    def __init__(self, reply_text: str = "summary", capture: bool = False,
                 stop_reason=None, replies=None):
        self.last_body = None
        self.bodies = []
        self._capture = capture
        if replies is not None:
            self._replies = list(replies)
        else:
            self._replies = [{"text": reply_text, "stop_reason": stop_reason}]
        self._idx = 0

    def request(self, method, path, body=None, headers=None):
        if self._capture:
            parsed = json.loads(body)
            self.bodies.append(parsed)
            self.last_body = parsed

    def getresponse(self):
        i = min(self._idx, len(self._replies) - 1)
        self._idx += 1
        reply = self._replies[i]
        events = [{
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": reply["text"]},
        }]
        if reply.get("stop_reason") is not None:
            events.append({
                "type": "message_delta",
                "delta": {"stop_reason": reply["stop_reason"]},
            })
        sse = "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()
        return FakeResponse(sse)

    def close(self):
        pass


class FakeUpstreamResponse:
    """Fake http.client.HTTPResponse for the ProxyHandler's upstream
    connection: chunked .read(n), plus .status/.reason/.getheaders()."""

    def __init__(self, status, reason, headers, body):
        self.status = status
        self.reason = reason
        self._headers = headers
        self._body = body
        self._pos = 0

    def getheaders(self):
        return self._headers

    def read(self, n=8192):
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


class FakeUpstreamConn:
    """Fake http.client.HTTPSConnection: request() is a no-op, getresponse()
    replays a pre-built FakeUpstreamResponse."""

    def __init__(self, response):
        self._response = response

    def request(self, *args, **kwargs):
        pass

    def getresponse(self):
        return self._response

    def close(self):
        pass


def seed_evictable(store):
    """Append a fresh, evictable (in_progress=False) entry under store._lock
    and run eviction, replicating the deleted CompressionStore.add()'s
    append+evict primitive. Unlike try_begin_compression(), this does NOT
    refuse when another entry is already in_progress -- it is the only way
    to apply eviction pressure while an in_progress entry is pinned, which
    is exactly what the eviction/cap tests need."""
    with store._lock:
        entry = store._new_entry()
        store._compressions.append(entry)
        store._evict_locked()
    return entry


def hermetic_home(testcase):
    """Point HOME at a fresh tempdir and strip the upstream-resolution env
    vars this machine exports, so tests see only what they write themselves.
    Shared by the tests/test_*upstream*.py and tests/test_loop_protection.py
    family -- see tests/test_chain_verb.py for the pattern this mirrors."""
    home = tempfile.mkdtemp(prefix="rolling-context-home-")
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    patch = mock.patch.dict(os.environ, {"HOME": home}, clear=False)
    patch.start()
    os.environ.pop("ANTHROPIC_BASE_URL", None)
    os.environ.pop("ROLLING_CONTEXT_UPSTREAM", None)
    os.environ.pop("ROLLING_CONTEXT_PORT", None)
    testcase.addCleanup(patch.stop)
    return home


def write_user_settings(home, env):
    path = os.path.join(home, ".claude", "settings.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"env": env}, f)


def start_listener(port, hits=None):
    """Spin up a real HTTPServer on 127.0.0.1:port; each POST appends `port`
    to `hits` (if given) and replays a stub /v1/messages 200 response."""
    log = hits if hits is not None else []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            log.append(port)
            body = b'{"type":"message","role":"assistant","content":[]}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def make_handler(body: bytes, headers: dict | None = None) -> ProxyHandler:
    """Build a ProxyHandler without going through socketserver's __init__."""
    handler = ProxyHandler.__new__(ProxyHandler)
    handler.request_version = "HTTP/1.1"
    handler.command = "POST"
    handler.path = "/v1/messages"
    handler.requestline = "POST /v1/messages HTTP/1.1"
    handler.client_address = ("127.0.0.1", 0)

    msg_headers = Message()
    msg_headers["content-length"] = str(len(body))
    for key, value in (headers or {}).items():
        msg_headers[key] = value
    handler.headers = msg_headers
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    return handler
