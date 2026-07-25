"""Task 2: FakeSummarizerConn emits stop_reason and a reply sequence.

Run: python3 -m unittest tests.test_fakes_stop_reason -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import compressor  # noqa: E402
sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeSummarizerConn  # noqa: E402


class FakeStopReasonTest(unittest.TestCase):
    def test_single_reply_with_stop_reason(self):
        conn = FakeSummarizerConn(reply_text="hi", stop_reason="max_tokens")
        body = conn.getresponse().read()
        text, sr = compressor.RollingCompressor()._parse_summary_sse(body)
        self.assertEqual(text, "hi")
        self.assertEqual(sr, "max_tokens")

    def test_reply_sequence_consumed_in_order(self):
        conn = FakeSummarizerConn(replies=[
            {"text": "first", "stop_reason": "max_tokens"},
            {"text": "second", "stop_reason": "end_turn"},
        ])
        comp = compressor.RollingCompressor()
        t1, s1 = comp._parse_summary_sse(conn.getresponse().read())
        t2, s2 = comp._parse_summary_sse(conn.getresponse().read())
        self.assertEqual((t1, s1), ("first", "max_tokens"))
        self.assertEqual((t2, s2), ("second", "end_turn"))

    def test_default_behavior_unchanged(self):
        conn = FakeSummarizerConn()
        text, sr = compressor.RollingCompressor()._parse_summary_sse(conn.getresponse().read())
        self.assertEqual(text, "summary")
        self.assertIsNone(sr)


if __name__ == "__main__":
    unittest.main()
