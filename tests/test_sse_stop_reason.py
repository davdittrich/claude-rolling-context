"""Task 1: _parse_summary_sse returns (text, stop_reason) and captures
message_delta.stop_reason from the native SSE stream.

Run: python3 -m unittest tests.test_sse_stop_reason -v
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import compressor  # noqa: E402


def _sse(*events: dict) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


class ParseSummarySseTest(unittest.TestCase):
    def setUp(self):
        self.comp = compressor.RollingCompressor()

    def test_captures_max_tokens_stop_reason(self):
        body = _sse(
            {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
        )
        text, stop_reason = self.comp._parse_summary_sse(body)
        self.assertEqual(text, "hello")
        self.assertEqual(stop_reason, "max_tokens")

    def test_end_turn_stop_reason(self):
        body = _sse(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "done"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        )
        text, stop_reason = self.comp._parse_summary_sse(body)
        self.assertEqual(text, "done")
        self.assertEqual(stop_reason, "end_turn")

    def test_empty_text_raises(self):
        body = _sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
        with self.assertRaises(RuntimeError):
            self.comp._parse_summary_sse(body)


if __name__ == "__main__":
    unittest.main()
