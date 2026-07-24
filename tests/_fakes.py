"""Shared test doubles for tests/test_*.py.

Deduplicates the copy-pasted `_FakeResponse`/`_FakeConn` (summarizer-facing)
and `FakeResponse`/`FakeConn`/`_make_handler` (ProxyHandler-facing) doubles
that were previously pasted verbatim across multiple test files. Each
call site's exact captured/returned bytes and assertions are unchanged --
these are parameterized to cover the observed variants (fixed vs. custom
reply text, request-body capture on/off).

tests/test_flattened_reply_guard.py keeps its own JSON-reply conn local (its
response shape is JSON, not SSE) but imports FakeResponse from here.

Gemini-e86.18. Room is intentionally left in this module for e86.16's
`seed_evictable(store)` store helper to be appended alongside these.

Run: python3 -m unittest discover -s tests
"""
import io
import json
import os
import sys
from email.message import Message

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
    """Fake `_summarizer_conn()` return value. `getresponse()` always emits
    a single content_block_delta SSE event carrying `reply_text` (default
    "summary", matching the historical hardcoded fixture used by the two
    call sites that never varied it). `capture` controls whether `request()`
    records the outgoing JSON body on `.last_body` (left None otherwise,
    matching the call sites that never inspected it)."""

    def __init__(self, reply_text: str = "summary", capture: bool = False):
        self.last_body = None
        self._reply_text = reply_text
        self._capture = capture

    def request(self, method, path, body=None, headers=None):
        if self._capture:
            self.last_body = json.loads(body)

    def getresponse(self):
        event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": self._reply_text}}
        sse = f"data: {json.dumps(event)}\n\n".encode()
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


def make_handler(body: bytes) -> ProxyHandler:
    """Build a ProxyHandler without going through socketserver's __init__."""
    handler = ProxyHandler.__new__(ProxyHandler)
    handler.request_version = "HTTP/1.1"
    handler.command = "POST"
    handler.path = "/v1/messages"
    handler.requestline = "POST /v1/messages HTTP/1.1"
    handler.client_address = ("127.0.0.1", 0)

    headers = Message()
    headers["content-length"] = str(len(body))
    handler.headers = headers
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    return handler
