"""
Rolling Context Compressor

When context exceeds trigger_tokens, compresses old messages down to target_tokens
of recent context + a dense chronological summary of everything before.

Two summarization modes:

1. NATIVE (default): clones the exact request Claude Code just sent — same
   model, system prompt, tools, and message history up to the cut point — and
   appends one user message asking for the summary. Because the request is
   byte-identical Claude Code session traffic, it passes Anthropic's
   subscription OAuth classification (issue #4), and because the prefix was
   just sent by the chat request, it's a prompt-cache read instead of full
   input cost.

2. FLATTENED: used when a custom summarizer is configured
   (ROLLING_CONTEXT_SUMMARIZER_URL / _KEY / _FORMAT). Flattens the
   conversation to text and sends a standalone request — Anthropic format or
   OpenAI chat-completions format, so any local model or third-party API
   works (Ollama, LM Studio, vLLM, OpenRouter, DeepSeek, ...).

Pure stdlib — no external dependencies.
"""

import collections
import copy
import gzip
import json
import os
import ssl
import time
import logging
import http.client
from urllib.parse import urlparse

log = logging.getLogger("rolling-context.compressor")

SUMMARIZER_API_KEY = os.environ.get("ROLLING_CONTEXT_SUMMARIZER_KEY") or ""
# "anthropic" (default) or "openai" — openai speaks /v1/chat/completions
SUMMARIZER_FORMAT = (os.environ.get("ROLLING_CONTEXT_SUMMARIZER_FORMAT") or "anthropic").lower()
# A pinned summarizer model or any custom summarizer config switches off
# native mode: native mode's whole value is that the cloned request reuses
# the session's own model, so it hits Anthropic's prompt cache. Pinning a
# different model guarantees a cache MISS, so treat it the same as a custom
# summarizer and fall back to a standalone flattened request.
SUMMARIZER_MODEL = os.environ.get("ROLLING_CONTEXT_MODEL") or ""
def native_mode():
    """True when the cloned-session-request path is usable, computed fresh."""
    return not (
        os.environ.get("ROLLING_CONTEXT_SUMMARIZER_URL")
        or os.environ.get("ROLLING_CONTEXT_SUMMARIZER_KEY")
        or (os.environ.get("ROLLING_CONTEXT_SUMMARIZER_FORMAT") or "anthropic").lower() != "anthropic"
        or os.environ.get("ROLLING_CONTEXT_MODEL")
    )
LEGACY_DEFAULT_MODEL = "claude-haiku-4-5-20251001"

ssl_ctx = ssl.create_default_context()

SummarizerEndpoint = collections.namedtuple("SummarizerEndpoint", "scheme host port path")


def summarizer_endpoint() -> "SummarizerEndpoint":
    """The summarizer's live endpoint (spec section 7): follows server's
    per-request current_upstream() unless ROLLING_CONTEXT_SUMMARIZER_URL is
    set, in which case that explicit override stays authoritative. A frozen
    summarizer URL would send compaction traffic to the old upstream while
    chat requests follow the new one -- half-working, which is the class of
    failure this whole design exists to end."""
    override = os.environ.get("ROLLING_CONTEXT_SUMMARIZER_URL")
    if override:
        parsed = urlparse(override)
        return SummarizerEndpoint(
            parsed.scheme,
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            parsed.path or "",
        )
    import server  # lazy: server.py imports from compressor at module load
    return server.current_upstream()


def _summarizer_conn(ep: "SummarizerEndpoint", timeout=600):
    """Create a connection to the summarizer server (same style as server.py)."""
    if ep.scheme == "https":
        return http.client.HTTPSConnection(ep.host, ep.port or 443, context=ssl_ctx, timeout=timeout)
    else:
        return http.client.HTTPConnection(ep.host, ep.port or 80, timeout=timeout)


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

def _clean_headers(headers: dict) -> dict:
    """Drop hop-by-hop/stale headers case-insensitively. The passthrough
    headers keep Claude Code's original casing (e.g. Accept-Encoding), so
    plain dict assignment of 'accept-encoding' would DUPLICATE the header and
    the upstream would still gzip the response."""
    drop = ("accept-encoding", "content-length", "host", "transfer-encoding", "connection")
    return {k: v for k, v in headers.items() if k.lower() not in drop}


SUMMARY_MARKER = "[ROLLING_CONTEXT_SUMMARY]"
HARD_CEILING_TOKENS = 20000  # native summary hard ceiling (max_tokens)
SUMMARY_END_MARKER = "[/ROLLING_CONTEXT_SUMMARY]"

SUMMARY_RULES = """RULES:
- Structure as a TIMELINE: use numbered steps showing what happened in order
- Preserve ALL file paths, function/class/variable names EXACTLY as written
- Preserve ALL technical decisions and WHY they were made
- Preserve ALL code changes: what file, what was changed, what the new code does
- Preserve ALL errors encountered and how they were resolved
- Preserve ALL user requests and instructions — what they asked for, what constraints they gave, what they said to do or NOT do
- Preserve user preferences, workflow choices, and recurring patterns (e.g. "always use X", "never do Y")
- Include key code snippets when they're central to understanding (keep them short)
- Do NOT editorialize or add commentary
- Be as DENSE as possible — every sentence should carry information

FORMAT:
## Active Goal
- [What the user is CURRENTLY asking for — their most recent request or focus]
- [Any constraints or rules the user has stated (do/don't do)]

## Previous Goals (completed or shifted away from)
- [Earlier goals that were finished or that the user moved on from — keep brief]

## Timeline
1. [First thing that happened]
2. [Second thing...]
...

## Current State
- [What's done, what's in progress, what's next]

## Key Details
- [File paths, configs, decisions that must not be forgotten]

BUDGET & DECAY:
- Keep the whole summary within ~16,000 tokens.
- INVARIANTS — never condense or drop: the ## Active Goal section, any stated user constraints (do/don't rules), and the ## Key Details section.
- The ## Timeline is the only section that may shrink. Keep the most recent ~15-20 steps detailed. As the summary approaches its budget, MERGE the OLDEST Timeline steps into denser milestone bullets rather than dropping the newest."""

# Native mode: appended as the final user message after the real conversation,
# like Claude Code's own /compact. Contains "context compressor" so test mocks
# can recognize summarization requests.
NATIVE_COMPACT_PROMPT = f"""Pause the current task. Act as a context compressor: produce a CHRONOLOGICAL, DENSE technical summary of the conversation above.

IMPORTANT: this compression request is NOT part of the conversation. Do not mention it in the summary, do not add it to the timeline, and do not treat it as the user's request. The Active Goal is the user's most recent REAL request from the conversation above — the task in progress continues after compression exactly where it left off, so summarize it as in-progress work, not as interrupted.

{SUMMARY_RULES}

If the conversation begins with a {SUMMARY_MARKER} block from an earlier compression, carry it forward and extend it. Preserve its ## Active Goal, stated user constraints, and ## Key Details at full fidelity — never condense or drop them. Keep recent ## Timeline entries detailed. As the combined summary approaches its ~16,000 token budget, MERGE the OLDEST Timeline entries into denser milestone bullets rather than dropping the newest events. Do not truncate the most recent entries.

Write ONLY the chronological summary, nothing else."""

# Flattened mode: standalone prompt carrying the conversation as text.
SUMMARIZE_PROMPT = f"""You are a context compressor for an AI coding assistant conversation.

Your job: take the conversation below and produce a CHRONOLOGICAL, DENSE technical summary.

{SUMMARY_RULES}

{{existing_summary_section}}

CONVERSATION TO COMPRESS:
{{conversation}}

Write the chronological summary:"""

CONDENSE_PROMPT = """The text below is a rolling conversation summary that exceeded its size budget. Rewrite it to fit within 16,000 tokens.

Preserve at full fidelity, never dropping: the ## Active Goal section, any stated user constraints or rules, and the ## Key Details section.
Compress the OLDEST ## Timeline entries by merging adjacent steps into denser milestone bullets. Never drop the newest entries.
Keep the same section headings and markdown structure. Output ONLY the rewritten summary, nothing else.

SUMMARY TO CONDENSE:
"""


class RollingCompressor:
    def __init__(
        self,
        trigger_tokens: int = 80000,
        target_tokens: int = 40000,
        summarizer_model: str = "",
        keep_turns: int = 8,
        keep_floor: int = 3,
    ):
        self.trigger_tokens = trigger_tokens
        self.target_tokens = target_tokens
        # Only used by flattened mode: native mode always uses the session's
        # own payload["model"] (that's what makes it a prompt-cache hit).
        # Setting ROLLING_CONTEXT_MODEL makes native_mode() (computed fresh each
        # call) return False, so
        # summarizer_model and native mode are mutually exclusive by design.
        self.summarizer_model = summarizer_model
        # Blend keep policy: keep between keep_floor and keep_turns recent
        # user-turns verbatim, with target_tokens as the soft char ceiling.
        # Clamp to guarantee 1 <= keep_floor <= keep_turns despite misconfig.
        self.keep_turns = max(1, keep_turns)
        self.keep_floor = max(1, min(keep_floor, self.keep_turns))
        self.compression_count = 0
        self.total_tokens_saved = 0
        # Most recent successful compression, for /health observability:
        # {ts, before_chars, after_chars, before_tokens}. None until the first.
        self.last_compression = None

    def _count_chars(self, messages: list) -> int:
        """Count total characters across all messages."""
        total_chars = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            total_chars += len(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            total_chars += len(json.dumps(block.get("input", {})))
                        elif block.get("type") == "tool_result":
                            c = block.get("content", "")
                            if isinstance(c, str):
                                total_chars += len(c)
                            elif isinstance(c, list):
                                for sub in c:
                                    if isinstance(sub, dict):
                                        total_chars += len(sub.get("text", ""))
        return total_chars

    def _find_keep_index(self, messages: list, keep_ratio: float) -> int:
        """Blend cut point: keep between keep_floor and keep_turns recent
        user-turns verbatim, with target chars (keep_ratio * total) as the soft
        upper ceiling that keep_floor may override.

        The returned index already satisfies both _safe_cut preconditions
        (clean user start AND clean predecessor), so _safe_cut is a no-op and
        the keep_turns cap cannot be exceeded by compress()'s later _safe_cut.
        """
        if len(messages) <= 4:
            return 0
        max_idx = len(messages) - 4
        total_chars = self._count_chars(messages)
        target_chars = int(total_chars * keep_ratio)
        # Boundaries that are BOTH a clean user start (no tool_result) AND have
        # a clean predecessor (no dangling tool_use) -> a _safe_cut no-op.
        boundaries = [
            i for i in range(len(messages))
            if messages[i].get("role") == "user"
            and not self._has_tool_result(messages[i])
            and (i == 0 or not self._has_tool_use(messages[i - 1]))
        ]
        if not boundaries:
            return 0
        # Walk boundaries newest -> oldest; extend while below the floor, or
        # below the turn cap and still within the char budget. Sum only the
        # newly included segment each step so every message is counted once.
        cut = boundaries[-1]
        turns_kept = 0
        accumulated = 0
        prev = len(messages)
        for b in reversed(boundaries):
            if turns_kept < self.keep_floor or (
                turns_kept < self.keep_turns and accumulated < target_chars
            ):
                cut = b
                accumulated += self._count_chars(messages[b:prev])
                prev = b
                turns_kept += 1
            else:
                break
        cut = min(cut, max_idx)
        if cut not in boundaries:
            # max_idx clipped into the last 4 msgs; snap to nearest clean boundary.
            cut = next((c for c in reversed(boundaries) if c <= max_idx), 0)
        return cut

    def _safe_cut(self, messages: list, cut: int, floor: int) -> int:
        """Walk cut back to a boundary where messages[cut:] is a valid start.

        Two rules, both enforced by the real API:
        - messages[cut] must be a plain 'user' message (no tool_result). If it's
          an assistant, a tool_result, or a 'system' directive, the injected
          prefix [summary(user), ack(assistant)] can't legally precede it — a
          system message in particular must sit between a user turn and a
          following assistant turn (user, system, assistant), so it can never
          be the first kept message.
        - messages[cut-1] (last summarized) must carry no tool_use, or its
          tool_results would be orphaned in the kept half.
        """
        while cut > floor:
            m = messages[cut]
            starts_clean = m.get("role") == "user" and not self._has_tool_result(m)
            prev_clean = not self._has_tool_use(messages[cut - 1])
            if starts_clean and prev_clean:
                return cut
            cut -= 1
        return cut

    def _has_tool_use(self, message: dict) -> bool:
        content = message.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    return True
        return False

    def _has_tool_result(self, message: dict) -> bool:
        content = message.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return True
        return False

    def _has_summary(self, messages: list) -> bool:
        if not messages:
            return False
        content = messages[0].get("content", "")
        if isinstance(content, str):
            return SUMMARY_MARKER in content
        return False

    def _extract_summary(self, messages: list) -> str:
        if not self._has_summary(messages):
            return ""
        content = messages[0].get("content", "")
        if isinstance(content, str) and SUMMARY_MARKER in content:
            start = content.find(SUMMARY_MARKER) + len(SUMMARY_MARKER)
            end = content.find(SUMMARY_END_MARKER)
            if end > start:
                return content[start:end].strip()
        return ""

    def _messages_to_text(self, messages: list) -> str:
        parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            name = block.get("name", "?")
                            inp = json.dumps(block.get("input", {}))
                            if len(inp) > 500:
                                inp = inp[:400] + "...[truncated]"
                            text_parts.append(f"[Tool: {name}({inp})]")
                        elif block.get("type") == "tool_result":
                            c = block.get("content", "")
                            if isinstance(c, str):
                                text_parts.append(f"[Result: {c[:1000]}]")
                            elif isinstance(c, list):
                                for sub in c:
                                    if isinstance(sub, dict):
                                        text_parts.append(f"[Result: {sub.get('text', '')[:1000]}]")
                text = "\n".join(text_parts)
            else:
                text = str(content)

            if len(text) > 4000:
                text = text[:3000] + "\n...[truncated]...\n" + text[-1000:]
            parts.append(f"**{role}**: {text}")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Native mode: clone the session's own request, append "compact this"
    # ------------------------------------------------------------------

    def _count_breakpoints(self, payload: dict, convo: list) -> int:
        """Count cache_control breakpoints across system, tools, and convo."""
        count = 0
        system = payload.get("system")
        if isinstance(system, list):
            for block in system:
                if isinstance(block, dict) and "cache_control" in block:
                    count += 1
        for tool in payload.get("tools") or []:
            if isinstance(tool, dict) and "cache_control" in tool:
                count += 1
        for msg in convo:
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "cache_control" in block:
                        count += 1
        return count

    def _parse_summary_sse(self, resp_body: bytes) -> tuple:
        """Parse a native summarizer SSE body into (text, stop_reason).

        stop_reason is captured from message_delta and is None if absent.
        Raises RuntimeError on a stream error event or empty text.
        """
        parts = []
        stop_reason = None
        for line in resp_body.decode("utf-8", errors="replace").split("\n"):
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            evt = data.get("type", "")
            if evt == "message_start":
                usage = data.get("message", {}).get("usage", {})
                log.info(
                    f"Native compaction usage: input={usage.get('input_tokens', 0):,} "
                    f"cache_read={usage.get('cache_read_input_tokens', 0):,} "
                    f"cache_write={usage.get('cache_creation_input_tokens', 0):,}"
                )
            elif evt == "content_block_delta":
                delta = data.get("delta", {})
                if delta.get("type") == "text_delta":
                    parts.append(delta.get("text", ""))
            elif evt == "message_delta":
                sr = data.get("delta", {}).get("stop_reason")
                if sr is not None:
                    stop_reason = sr
            elif evt == "error":
                raise RuntimeError(f"Summarization stream error: {json.dumps(data)[:500]}")
        summary = "".join(parts).strip()
        if not summary:
            snippet = resp_body.decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"Summarization returned empty text; response starts: {snippet}")
        return summary, stop_reason

    def _condense_summary(self, summary_text: str, auth_headers: dict, model: str) -> str:
        """Single-pass backstop: re-summarize an over-budget summary under the
        soft target, preserving invariants and folding the oldest Timeline.
        Uses a standalone request (no cache dependency — rare guard path)."""
        body = {
            "model": model,
            "max_tokens": 20000,
            "stream": True,
            "messages": [{"role": "user", "content": CONDENSE_PROMPT + summary_text}],
        }
        req_body = json.dumps(body).encode()
        headers = _clean_headers(auth_headers)
        headers["content-length"] = str(len(req_body))
        headers["accept-encoding"] = "identity"
        ep = summarizer_endpoint()
        summarizer_path = _join_path(ep.path, "/v1/messages")
        log.info(f"Summary over budget -> condense pass ({len(summary_text):,} chars)")
        conn = _summarizer_conn(ep)
        conn.request("POST", summarizer_path, body=req_body, headers=headers)
        resp = conn.getresponse()
        resp_body = resp.read()
        conn.close()
        if resp.status != 200:
            err = resp_body.decode("utf-8", errors="replace")
            raise RuntimeError(f"Summarization API returned {resp.status}: {err[:500]}")
        if resp_body[:2] == b"\x1f\x8b":
            resp_body = gzip.decompress(resp_body)
        text, _sr = self._parse_summary_sse(resp_body)
        return text

    def _summarize_native(self, payload: dict, messages: list, cut: int, auth_headers: dict) -> str:
        """Send the session's own request shape with a compact instruction.

        The conversation prefix is identical to what Claude Code just sent, so
        upstream serves it from the prompt cache, and subscription OAuth
        classification sees genuine Claude Code session traffic.
        """
        convo = list(messages[:cut])

        # Place a cache breakpoint on the last conversation message (budget
        # permitting, max 4 per request) so the lookup reads the deepest
        # cache entry created by earlier chat requests.
        privatized = False
        if convo and self._count_breakpoints(payload, convo) < 4:
            last = copy.deepcopy(convo[-1])
            c = last.get("content")
            if isinstance(c, str):
                last["content"] = [{
                    "type": "text",
                    "text": c,
                    "cache_control": {"type": "ephemeral"},
                }]
            elif isinstance(c, list) and c and isinstance(c[-1], dict):
                c[-1]["cache_control"] = {"type": "ephemeral"}
            convo[-1] = last
            privatized = True

        # Append the compact instruction. If the conversation already ends on
        # a user turn, merge it into that message's content instead of
        # appending a new user message — two consecutive user turns is a
        # 400 from the API. convo[-1] is only a private copy if the
        # breakpoint block above ran (privatized); otherwise it is still the
        # caller's own dict aliased in from `messages`, so it must still be
        # deep-copied here rather than mutated in place.
        if convo and convo[-1].get("role") == "user":
            merged_last = convo[-1] if privatized else copy.deepcopy(convo[-1])
            content = merged_last.get("content", "")
            blocks = list(content) if isinstance(content, list) else (
                [{"type": "text", "text": content}] if content else []
            )
            blocks.append({"type": "text", "text": NATIVE_COMPACT_PROMPT})
            merged_last["content"] = blocks
            convo[-1] = merged_last
            compact_messages = convo
        else:
            compact_messages = convo + [{"role": "user", "content": NATIVE_COMPACT_PROMPT}]

        model = payload.get("model", LEGACY_DEFAULT_MODEL)
        max_tokens = HARD_CEILING_TOKENS
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "stream": True,
            "messages": compact_messages,
        }
        for key in ("system", "tools", "metadata"):
            if payload.get(key) is not None:
                body[key] = payload[key]
        if body.get("tools"):
            # The summary must be text — without this the model may answer
            # the cloned request with a tool_use and the summary comes back empty
            body["tool_choice"] = {"type": "none"}
        thinking = payload.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "enabled":
            body["thinking"] = thinking
            body["max_tokens"] = max(max_tokens, int(thinking.get("budget_tokens", 0)) + 4000)

        req_body = json.dumps(body).encode()
        headers = _clean_headers(auth_headers)
        headers["content-length"] = str(len(req_body))
        headers["accept-encoding"] = "identity"

        ep = summarizer_endpoint()
        summarizer_path = _join_path(ep.path, "/v1/messages")
        log.info(
            f"Native compaction request -> {ep.scheme}://{ep.host}:{ep.port} "
            f"model={model} messages={len(body['messages'])} ({len(req_body):,} bytes)"
        )

        conn = _summarizer_conn(ep)
        conn.request("POST", summarizer_path, body=req_body, headers=headers)
        resp = conn.getresponse()
        resp_body = resp.read()
        conn.close()
        if resp_body[:2] == b"\x1f\x8b":  # upstream gzipped despite identity
            resp_body = gzip.decompress(resp_body)

        if resp.status != 200:
            error = resp_body.decode("utf-8", errors="replace")
            raise RuntimeError(f"Summarization API returned {resp.status}: {error[:500]}")

        summary, stop_reason = self._parse_summary_sse(resp_body)
        over_ceiling = len(summary) > HARD_CEILING_TOKENS * 4  # ~4 chars/token estimate
        if stop_reason == "max_tokens" or over_ceiling:
            log.info(
                f"Summary guard fired (stop_reason={stop_reason}, "
                f"chars={len(summary):,}) -> condense pass"
            )
            summary = self._condense_summary(summary, auth_headers, model)
            if len(summary) > HARD_CEILING_TOKENS * 4:
                log.warning(
                    f"Summary still over budget after condense "
                    f"({len(summary):,} chars)"
                )
        return summary

    # ------------------------------------------------------------------
    # Flattened mode: standalone request to a custom summarizer
    # ------------------------------------------------------------------

    def _summarize_flattened_once(self, prompt: str, auth_headers: dict) -> tuple:
        """Single flattened summarization request. Returns (text, truncated),
        where truncated is True if the upstream stopped at the token cap
        (openai finish_reason == "length" / anthropic stop_reason ==
        "max_tokens"). No guard/retry here — the caller applies the guard."""
        summary_max_tokens = 20000
        model = self.summarizer_model or LEGACY_DEFAULT_MODEL
        ep = summarizer_endpoint()

        if SUMMARIZER_FORMAT == "openai":
            if not self.summarizer_model:
                raise RuntimeError(
                    "ROLLING_CONTEXT_SUMMARIZER_FORMAT=openai requires "
                    "ROLLING_CONTEXT_MODEL to name the summarizer model"
                )
            path = _join_path(ep.path, "/v1/chat/completions")
            req_body = json.dumps({
                "model": model,
                "max_tokens": summary_max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }).encode()
            headers = {"content-type": "application/json"}
            if SUMMARIZER_API_KEY:
                headers["authorization"] = f"Bearer {SUMMARIZER_API_KEY}"
        else:
            path = _join_path(ep.path, "/v1/messages")
            req_body = json.dumps({
                "model": model,
                "max_tokens": summary_max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }).encode()
            if SUMMARIZER_API_KEY:
                headers = {
                    "content-type": "application/json",
                    "anthropic-version": "2023-06-01",
                    "x-api-key": SUMMARIZER_API_KEY,
                }
            else:
                headers = _clean_headers(auth_headers)
        headers["content-length"] = str(len(req_body))
        headers["accept-encoding"] = "identity"

        log.info(
            f"Compression request -> {ep.scheme}://{ep.host}:{ep.port} path={path} "
            f"format={SUMMARIZER_FORMAT} model={model}"
        )

        conn = _summarizer_conn(ep, timeout=120)
        conn.request("POST", path, body=req_body, headers=headers)
        resp = conn.getresponse()
        resp_body = resp.read()
        conn.close()
        if resp_body[:2] == b"\x1f\x8b":  # upstream gzipped despite identity
            resp_body = gzip.decompress(resp_body)

        if resp.status != 200:
            error = resp_body.decode("utf-8", errors="replace")
            raise RuntimeError(f"Summarization API returned {resp.status}: {error[:500]}")
        data = json.loads(resp_body)

        def _empty_reply_error(detail: str) -> RuntimeError:
            snippet = resp_body.decode("utf-8", errors="replace")[:300]
            return RuntimeError(f"Summarization returned {detail}; response starts: {snippet}")

        if SUMMARIZER_FORMAT == "openai":
            choices = data.get("choices") or []
            if not choices:
                raise _empty_reply_error("no choices")
            content = (choices[0].get("message") or {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise _empty_reply_error("empty text")
            truncated = choices[0].get("finish_reason") == "length"
            return content, truncated

        content_blocks = data.get("content") or []
        if not content_blocks:
            raise _empty_reply_error("no content blocks")
        text = content_blocks[0].get("text")
        if not isinstance(text, str) or not text.strip():
            raise _empty_reply_error("empty text")
        truncated = data.get("stop_reason") == "max_tokens"
        return text, truncated

    def _summarize_flattened(self, prompt: str, auth_headers: dict) -> str:
        """Flattened summarization with the oldest-first decay guard: if the
        first pass truncated at the cap or overflows the hard ceiling, run one
        condense pass (same wire format, no recursion) folding the oldest
        Timeline. Mirrors the native guard in _summarize_native."""
        text, truncated = self._summarize_flattened_once(prompt, auth_headers)
        if truncated or len(text) > HARD_CEILING_TOKENS * 4:
            log.info(
                f"Flattened summary guard fired (truncated={truncated}, "
                f"chars={len(text):,}) -> condense pass"
            )
            text, _ = self._summarize_flattened_once(CONDENSE_PROMPT + text, auth_headers)
            if len(text) > HARD_CEILING_TOKENS * 4:
                log.warning(
                    f"Flattened summary still over budget after condense "
                    f"({len(text):,} chars)"
                )
        return text

    # ------------------------------------------------------------------

    def compress(self, messages: list, auth_headers: dict, real_token_count: int = None,
                 payload: dict = None) -> list:
        """Compress messages using rolling summarization (synchronous).

        Returns the compressed message list, or None when there is nothing
        worth compressing (callers must not build a compression entry then)."""
        # Use real API token count to determine what fraction of content to keep
        if real_token_count and real_token_count > 0:
            keep_ratio = self.target_tokens / real_token_count
            log.info(
                f"Keep ratio: {keep_ratio:.1%} "
                f"(target={self.target_tokens:,} / real={real_token_count:,})"
            )
        else:
            # Fallback: keep half (conservative)
            keep_ratio = 0.5
            log.info(f"Keep ratio: {keep_ratio:.1%} (fallback, no real token count)")

        keep_from_idx = self._find_keep_index(messages, keep_ratio)

        has_existing_summary = self._has_summary(messages)
        start_idx = 2 if has_existing_summary else 0

        keep_from_idx = self._safe_cut(messages, keep_from_idx, start_idx)

        if keep_from_idx <= start_idx:
            log.info("Not enough old messages to compress, passing through")
            return None

        recent_messages = messages[keep_from_idx:]

        use_native = native_mode() and payload is not None
        if use_native:
            new_summary = self._summarize_native(payload, messages, keep_from_idx, auth_headers)
        else:
            existing_summary = self._extract_summary(messages) if has_existing_summary else ""
            to_compress = messages[start_idx:keep_from_idx]
            if not to_compress:
                log.info("Nothing to compress")
                return None
            conversation_text = self._messages_to_text(to_compress)
            existing_section = ""
            if existing_summary:
                existing_section = (
                    "EXISTING ROLLING SUMMARY FROM PREVIOUS COMPRESSIONS "
                    "(carry it forward and extend it; preserve ## Active Goal, "
                    "user constraints, and ## Key Details at full fidelity; as "
                    "the summary nears ~16,000 tokens, merge the OLDEST Timeline "
                    "entries into denser bullets rather than dropping the "
                    "newest):\n"
                    f"{existing_summary}\n\n"
                )
            prompt = SUMMARIZE_PROMPT.format(
                existing_summary_section=existing_section,
                conversation=conversation_text,
            )
            log.info(
                f"Summarizing {keep_from_idx - start_idx} messages "
                f"({len(conversation_text):,} chars, flattened)..."
            )
            new_summary = self._summarize_flattened(prompt, auth_headers)

        log.info(f"Summary generated: {len(new_summary):,} chars")

        summary_message = {
            "role": "user",
            "content": (
                f"{SUMMARY_MARKER}\n"
                f"{new_summary}\n"
                f"{SUMMARY_END_MARKER}\n\n"
                "The above is a chronological summary of our earlier conversation. "
                "All file paths, decisions, and code changes are preserved. "
                "Continue from where we left off."
            ),
        }
        ack_message = {
            "role": "assistant",
            "content": (
                "I have the full context from our previous conversation — "
                "the timeline, all files modified, decisions made, and current state. "
                "Continuing from where we left off."
            ),
        }

        compressed = [summary_message, ack_message] + recent_messages

        # original_chars needs its own pass over `messages`. compressed_chars
        # is additive over compress's two parts, so scan recent_messages once
        # (recent_chars) and derive compressed_chars from it instead of
        # re-scanning recent_messages a second time inside `compressed`.
        original_chars = self._count_chars(messages)
        recent_chars = self._count_chars(recent_messages)
        prefix_chars = self._count_chars([summary_message, ack_message])
        compressed_chars = prefix_chars + recent_chars
        summary_chars = len(new_summary)
        self.compression_count += 1
        if real_token_count:
            # Real token count is known, but reduction is only a char-based
            # ratio (compressed/original chars), not a token-based one, so
            # estimated_output_tokens is an ESTIMATE derived from that ratio,
            # not an exact token count.
            reduction = compressed_chars / original_chars if original_chars > 0 else 0
            estimated_output_tokens = int(real_token_count * reduction)
            self.total_tokens_saved += real_token_count - estimated_output_tokens
            log.info(
                f"Compression #{self.compression_count}: "
                f"~{real_token_count:,} -> ~{estimated_output_tokens:,} real tokens "
                f"({reduction:.0%} of original, "
                f"summary={summary_chars:,} chars, recent={recent_chars:,} chars)"
            )
        else:
            # No real token count available; estimate tokens saved from raw
            # char delta using ~4 chars/token (English-text average for
            # Claude's tokenizer) rather than the prior //2 (~2 chars/token),
            # which over-reported savings by roughly 2x.
            self.total_tokens_saved += (original_chars - compressed_chars) // 4
            log.info(
                f"Compression #{self.compression_count}: "
                f"{original_chars:,} -> {compressed_chars:,} chars "
                f"(summary={summary_chars:,}, recent={recent_chars:,})"
            )

        # Record this compression for /health. Chars are exact (both sides);
        # before_tokens is the real trigger count (0 when unknown). No
        # after-token: the post-compression token count is only an estimate.
        self.last_compression = {
            "ts": time.time(),
            "before_chars": original_chars,
            "after_chars": compressed_chars,
            "before_tokens": real_token_count or 0,
        }

        return compressed
