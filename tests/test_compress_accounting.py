"""Tests for Gemini-e86.12: compress()'s accounting block in
proxy/compressor.py.

Regression coverage for commit 7925f01, which fixed two accounting bugs and
had no test:
1. The fallback (no real_token_count) total_tokens_saved divisor was //2
   (~2 chars/token), over-reporting savings ~2x; fixed to //4.
2. compressed_chars was computed by re-scanning `compressed` (a second full
   _count_chars pass that duplicates the recent_messages scan already done
   for recent_chars); fixed to sum prefix_chars + recent_chars instead.

Neither bug changes compression OUTPUT (cut point, summary, kept messages)
-- both are log/accounting-only, which is why they shipped without a test.

Run: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import compressor  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeSummarizerConn  # noqa: E402


def _build_messages():
    """9 messages shaped like test_native_summary_carry_forward's fixture:
    with keep_floor=2/keep_turns=2, compress()'s keep_floor rule alone
    (independent of the fallback keep_ratio=0.5 target) saturates at
    boundaries 8 and 6, so the cut always lands on index 4 regardless of
    messages[0:2]'s content -- messages[4:] (5 messages) becomes
    recent_messages, messages[:4] gets summarized. Fixed repeat counts make
    every char count, and therefore total_tokens_saved, reproducible."""
    return [
        {"role": "user", "content": "prior turn 0 " * 10},
        {"role": "assistant", "content": "prior turn 1 " * 10},
        {"role": "user", "content": "message 2 " * 50},
        {"role": "assistant", "content": "message 3 " * 50},
        {"role": "user", "content": "message 4 " * 50},
        {"role": "assistant", "content": "message 5 " * 50},
        {"role": "user", "content": "message 6 " * 50},
        {"role": "assistant", "content": "message 7 " * 50},
        {"role": "user", "content": "message 8 (most recent)"},
    ]


class CompressAccountingTest(unittest.TestCase):
    def setUp(self):
        self._real_conn_fn = compressor._summarizer_conn
        self._fake_conn = FakeSummarizerConn(reply_text="mocked deterministic summary text.")
        compressor._summarizer_conn = lambda timeout=600: self._fake_conn

    def tearDown(self):
        compressor._summarizer_conn = self._real_conn_fn

    def test_fallback_tokens_saved_uses_div4_not_div2(self):
        """Drives compress() with real_token_count=None (forces the fallback
        accounting branch) and proves total_tokens_saved == (original_chars
        - compressed_chars) // 4 -- a revert to // 2 fails this assertion."""
        comp = compressor.RollingCompressor(keep_floor=2, keep_turns=2)
        messages = _build_messages()
        payload = {"model": "claude-sonnet-4-5-20250929"}

        original_chars = comp._count_chars(messages)

        result = comp.compress(messages, auth_headers={}, real_token_count=None, payload=payload)
        self.assertIsNotNone(result)

        compressed_chars = comp._count_chars(result)
        expected_saved = (original_chars - compressed_chars) // 4

        self.assertEqual(comp.total_tokens_saved, expected_saved)

    def test_compressed_chars_additive_equals_full_rescan(self):
        """Guards the 'compute once' refactor: prefix_chars + recent_chars
        (what compress() now sums to get compressed_chars) must equal a full
        _count_chars scan over the assembled result. result ==
        [summary_message, ack_message] + recent_messages."""
        comp = compressor.RollingCompressor(keep_floor=2, keep_turns=2)
        messages = _build_messages()
        payload = {"model": "claude-sonnet-4-5-20250929"}

        result = comp.compress(messages, auth_headers={}, real_token_count=None, payload=payload)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 7)  # [summary, ack] + 5 recent messages

        prefix = result[:2]
        recent_messages = result[2:]

        additive = comp._count_chars(prefix) + comp._count_chars(recent_messages)
        full_rescan = comp._count_chars(result)

        self.assertEqual(additive, full_rescan)


if __name__ == "__main__":
    unittest.main()
