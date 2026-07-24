"""SSE usage-parse must only json.loads the events it actually needs
(message_start / message_delta), not every `data:` line, and must split the
buffered SSE text exactly once.

Bug: `_handle_messages` ran `text.split("\n")` then `json.loads()` on EVERY
`data:` line of a streamed completion (which can be thousands of events for a
long response) purely to read usage off the first (`message_start`) and last
(`message_delta`) events, then did a second `text.split("\n")` when
`total_input == 0`. Fix: cheap substring check before `json.loads`, single
split, early-exit once usage is captured.

This test drives `_handle_messages` end-to-end against a fake upstream
connection returning a synthetic streaming SSE body with many
`content_block_delta` events plus one `message_start` and one
`message_delta`, and asserts:
  1. `total_input` is identical to what the old parse-every-line logic would
     produce (message_start usage sum, respecting the message_delta
     `> total_input` guard).
  2. `json.loads` is called far fewer times than the number of SSE events
     (spy/count via `unittest.mock.patch(..., wraps=json.loads)`).

Run: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeUpstreamConn, FakeUpstreamResponse, make_handler  # noqa: E402

N_CONTENT_BLOCK_DELTAS = 500


def _sse_line(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _build_sse_body() -> bytes:
    parts = [
        _sse_line({
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 5000,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 50,
                }
            },
        }),
    ]
    for i in range(N_CONTENT_BLOCK_DELTAS):
        parts.append(_sse_line({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": f"tok{i}"},
        }))
    # message_delta with no input_tokens (the common, real-Anthropic shape) —
    # must NOT clobber the total_input the message_start branch already set.
    parts.append(_sse_line({
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": 42},
    }))
    parts.append(_sse_line({"type": "message_stop"}))
    return "".join(parts).encode()


class SSEUsageParseTest(unittest.TestCase):
    def test_total_input_correct_and_json_loads_count_low(self):
        request_body = json.dumps({
            "model": "claude-x",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()
        handler = make_handler(request_body)

        upstream_body = _build_sse_body()
        fake_resp = FakeUpstreamResponse(
            200, "OK",
            [("content-type", "text/event-stream")],
            upstream_body,
        )
        fake_conn = FakeUpstreamConn(fake_resp)

        with patch("server._upstream_conn", return_value=fake_conn):
            with patch("server.json.loads", wraps=json.loads) as loads_spy:
                with self.assertLogs("rolling-context", level="INFO") as cm:
                    handler._handle_messages()

        log_text = "\n".join(cm.output)

        # (1) Correctness: identical total_input to the old per-line logic —
        # message_start usage sum (5000+100+50=5150); message_delta has no
        # input_tokens so tokens=0 and the `> total_input` guard keeps 5150.
        self.assertIn("Input tokens from message_start: 5,150", log_text)
        self.assertNotIn("Input tokens from message_delta", log_text)

        # (2) Efficiency: json.loads must NOT be called once per SSE line.
        # Old logic: 1 (request body) + one per `data:` line (502) = 503.
        # New logic parses only message_start + message_delta events.
        self.assertLess(
            loads_spy.call_count, 10,
            f"json.loads called {loads_spy.call_count} times for "
            f"{N_CONTENT_BLOCK_DELTAS + 2} SSE events — expected only "
            "message_start/message_delta to be parsed",
        )

    def test_message_delta_guard_still_applies(self):
        """Guard: a converter-style message_delta usage.input_tokens larger
        than the message_start total must still win (`> total_input`)."""
        request_body = json.dumps({
            "model": "claude-x",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()
        handler = make_handler(request_body)

        parts = [
            _sse_line({
                "type": "message_start",
                "message": {"usage": {"input_tokens": 100}},
            }),
            _sse_line({
                "type": "message_delta",
                "usage": {"input_tokens": 9000},
            }),
        ]
        upstream_body = "".join(parts).encode()
        fake_resp = FakeUpstreamResponse(
            200, "OK",
            [("content-type", "text/event-stream")],
            upstream_body,
        )
        fake_conn = FakeUpstreamConn(fake_resp)

        with patch("server._upstream_conn", return_value=fake_conn):
            with self.assertLogs("rolling-context", level="INFO") as cm:
                handler._handle_messages()

        log_text = "\n".join(cm.output)
        self.assertIn("Input tokens from message_start: 100", log_text)
        self.assertIn("Input tokens from message_delta: 9,000", log_text)


if __name__ == "__main__":
    unittest.main()
