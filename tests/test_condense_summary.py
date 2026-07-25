"""Task 3: _condense_summary sends CONDENSE_PROMPT + summary and returns
the condensed text.

Run: python3 -m unittest tests.test_condense_summary -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import compressor  # noqa: E402
sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeSummarizerConn  # noqa: E402


class CondenseSummaryTest(unittest.TestCase):
    def setUp(self):
        self._real = compressor._summarizer_conn
        self._fake = FakeSummarizerConn(reply_text="CONDENSED", capture=True)
        compressor._summarizer_conn = lambda timeout=600: self._fake

    def tearDown(self):
        compressor._summarizer_conn = self._real

    def test_returns_condensed_text(self):
        comp = compressor.RollingCompressor()
        out = comp._condense_summary("OVERLONG SUMMARY TEXT", auth_headers={},
                                     model="claude-sonnet-4-5-20250929")
        self.assertEqual(out, "CONDENSED")

    def test_sends_condense_prompt_and_summary(self):
        comp = compressor.RollingCompressor()
        comp._condense_summary("UNIQUE_SUMMARY_MARKER", auth_headers={},
                               model="claude-sonnet-4-5-20250929")
        sent = self._fake.last_body
        blob = "".join(
            b.get("text", "") if isinstance(b, dict) else b
            for m in sent["messages"]
            for b in ([m["content"]] if isinstance(m["content"], str) else m["content"])
        )
        self.assertIn("UNIQUE_SUMMARY_MARKER", blob)
        self.assertIn("16,000", compressor.CONDENSE_PROMPT)
        self.assertEqual(sent["max_tokens"], 20000)


class _Non200Conn:
    def request(self, *a, **k):
        pass

    def getresponse(self):
        from _fakes import FakeResponse
        return FakeResponse(b'{"error":"bad request"}', status=400)

    def close(self):
        pass


class CondenseNon200Test(unittest.TestCase):
    def setUp(self):
        self._real = compressor._summarizer_conn
        compressor._summarizer_conn = lambda timeout=600: _Non200Conn()

    def tearDown(self):
        compressor._summarizer_conn = self._real

    def test_non_200_raises_with_status(self):
        comp = compressor.RollingCompressor()
        with self.assertRaises(RuntimeError) as ctx:
            comp._condense_summary("x", auth_headers={}, model="claude-sonnet-4-5-20250929")
        self.assertIn("400", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
