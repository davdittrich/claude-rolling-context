"""Non-streaming responses must parse real token usage, not fall back to the
chars//4 estimate.

Bug: `buffer += chunk` was gated by `if is_streaming:`, so for stream:false
requests `buffer` stayed b"" and the `elif not is_streaming and buffer:` JSON
parse branch below was dead code — total_input always fell through to the
rough chars-based estimate. Fix: accumulate `buffer` unconditionally so the
non-streaming branch has a body to parse, same as the streaming SSE path
already did.

This test drives `_handle_messages` end-to-end against a fake upstream
connection returning a synthetic non-streaming JSON body with a real `usage`
block, and asserts (via the method's own log lines, since total_input is a
local) that the parsed usage was used and the estimate fallback did not fire.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeUpstreamConn, FakeUpstreamResponse, make_handler  # noqa: E402


class NonStreamingUsageParseTest(unittest.TestCase):
    def test_real_usage_parsed_not_estimated(self):
        request_body = json.dumps({
            "model": "claude-x",
            "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()
        handler = make_handler(request_body)

        # Real usage the char estimate (2 chars // 4 == 0) would never produce.
        upstream_body = json.dumps({
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "hello"}],
            "usage": {
                "input_tokens": 5000,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 50,
                "output_tokens": 10,
            },
        }).encode()
        fake_resp = FakeUpstreamResponse(
            200, "OK",
            [("content-type", "application/json"),
             ("content-length", str(len(upstream_body)))],
            upstream_body,
        )
        fake_conn = FakeUpstreamConn(fake_resp)

        with patch("server._upstream_conn", return_value=fake_conn):
            with self.assertLogs("rolling-context", level="INFO") as cm:
                handler._handle_messages()

        log_text = "\n".join(cm.output)
        self.assertIn(
            "Input tokens from response: 5,150", log_text,
            "expected the JSON usage branch to fire with the real total",
        )
        self.assertNotIn(
            "estimating from chars", log_text,
            "must not fall back to the chars//4 estimate when usage is present",
        )

        # Client still gets the upstream body verbatim (parse-after-forward),
        # trailing the status line and forwarded headers.
        handler.wfile.seek(0)
        self.assertTrue(handler.wfile.read().endswith(upstream_body))

    def test_estimate_still_used_when_usage_absent(self):
        """Guard: the estimate fallback must still fire for a genuinely
        usage-less body — the fix must not remove the fallback, only stop it
        from masking real usage."""
        request_body = json.dumps({
            "model": "claude-x",
            "stream": False,
            "messages": [{"role": "user", "content": "hello there, this needs enough chars"}],
        }).encode()
        handler = make_handler(request_body)

        upstream_body = json.dumps({
            "id": "msg_2",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
        }).encode()
        fake_resp = FakeUpstreamResponse(
            200, "OK",
            [("content-type", "application/json"),
             ("content-length", str(len(upstream_body)))],
            upstream_body,
        )
        fake_conn = FakeUpstreamConn(fake_resp)

        with patch("server._upstream_conn", return_value=fake_conn):
            with self.assertLogs("rolling-context", level="INFO") as cm:
                handler._handle_messages()

        log_text = "\n".join(cm.output)
        self.assertIn("estimating from chars", log_text)


if __name__ == "__main__":
    unittest.main()
