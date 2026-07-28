"""Task 4: native guard runs exactly one condense pass on truncation or
over-ceiling, and leaves normal summaries untouched.

Run: python3 -m unittest tests.test_summary_decay_guard -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import compressor  # noqa: E402
sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeSummarizerConn  # noqa: E402

PAYLOAD = {"model": "claude-sonnet-4-5-20250929"}
MESSAGES = [
    {"role": "user", "content": "u1 " * 50},
    {"role": "assistant", "content": "a1 " * 50},
    {"role": "user", "content": "u2 (recent)"},
]


class DecayGuardTest(unittest.TestCase):
    def _patch(self, fake):
        self._real = compressor._summarizer_conn
        compressor._summarizer_conn = lambda ep, timeout=600: fake
        self.addCleanup(lambda: setattr(compressor, "_summarizer_conn", self._real))

    def test_truncation_triggers_one_condense_pass(self):
        fake = FakeSummarizerConn(replies=[
            {"text": "TRUNCATED SUMMARY", "stop_reason": "max_tokens"},
            {"text": "CONDENSED SUMMARY", "stop_reason": "end_turn"},
        ], capture=True)
        self._patch(fake)
        comp = compressor.RollingCompressor(keep_floor=1, keep_turns=1)
        out = comp._summarize_native(PAYLOAD, MESSAGES, cut=2, auth_headers={})
        self.assertEqual(out, "CONDENSED SUMMARY")
        self.assertEqual(len(fake.bodies), 2)  # main + one condense

    def test_over_ceiling_by_size_triggers_condense(self):
        huge = "X" * (compressor.HARD_CEILING_TOKENS * 4 + 10)
        fake = FakeSummarizerConn(replies=[
            {"text": huge, "stop_reason": "end_turn"},
            {"text": "CONDENSED", "stop_reason": "end_turn"},
        ], capture=True)
        self._patch(fake)
        comp = compressor.RollingCompressor(keep_floor=1, keep_turns=1)
        out = comp._summarize_native(PAYLOAD, MESSAGES, cut=2, auth_headers={})
        self.assertEqual(out, "CONDENSED")
        self.assertEqual(len(fake.bodies), 2)

    def test_normal_summary_no_condense(self):
        fake = FakeSummarizerConn(reply_text="FINE SUMMARY",
                                  stop_reason="end_turn", capture=True)
        self._patch(fake)
        comp = compressor.RollingCompressor(keep_floor=1, keep_turns=1)
        out = comp._summarize_native(PAYLOAD, MESSAGES, cut=2, auth_headers={})
        self.assertEqual(out, "FINE SUMMARY")
        self.assertEqual(len(fake.bodies), 1)  # main only, no condense

    def test_native_cap_is_20000(self):
        fake = FakeSummarizerConn(reply_text="ok", stop_reason="end_turn", capture=True)
        self._patch(fake)
        comp = compressor.RollingCompressor(keep_floor=1, keep_turns=1)
        comp._summarize_native(PAYLOAD, MESSAGES, cut=2, auth_headers={})
        self.assertEqual(fake.bodies[0]["max_tokens"], 20000)


if __name__ == "__main__":
    unittest.main()
