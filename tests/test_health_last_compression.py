"""zd2.3: compressor records last_compression; /health surfaces it.

last_compression = {ts, before_chars, after_chars, before_tokens} — exact
chars both sides, real trigger tokens (0 when unknown), no after-token
(post-compression tokens are only estimated), no count (top-level
compression_count already provides it). None until the first compression;
never set on a no-op (return None) compression.

Run: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import compressor  # noqa: E402
import server  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeSummarizerConn, make_handler  # noqa: E402

import json  # noqa: E402


def _messages():
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


def _health_json():
    handler = make_handler(b"")
    handler._handle_health()
    body = handler.wfile.getvalue().split(b"\r\n\r\n", 1)[1]
    return json.loads(body)


class LastCompressionTest(unittest.TestCase):
    def setUp(self):
        self._real = compressor._summarizer_conn
        compressor._summarizer_conn = lambda timeout=600: FakeSummarizerConn(
            reply_text="mocked deterministic summary text.")

    def tearDown(self):
        compressor._summarizer_conn = self._real

    def test_set_on_success_with_real_tokens(self):
        comp = compressor.RollingCompressor(keep_floor=2, keep_turns=2)
        self.assertIsNone(comp.last_compression)
        msgs = _messages()
        original = comp._count_chars(msgs)
        result = comp.compress(msgs, auth_headers={}, real_token_count=5000,
                               payload={"model": "claude-sonnet-4-5-20250929"})
        self.assertIsNotNone(result)
        lc = comp.last_compression
        self.assertEqual(set(lc), {"ts", "before_chars", "after_chars", "before_tokens"})
        self.assertEqual(lc["before_chars"], original)
        self.assertEqual(lc["after_chars"], comp._count_chars(result))
        self.assertEqual(lc["before_tokens"], 5000)
        self.assertIsInstance(lc["ts"], float)
        self.assertNotIn("after_tokens", lc)
        self.assertNotIn("count", lc)

    def test_before_tokens_zero_on_fallback(self):
        comp = compressor.RollingCompressor(keep_floor=2, keep_turns=2)
        comp.compress(_messages(), auth_headers={}, real_token_count=None,
                      payload={"model": "claude-sonnet-4-5-20250929"})
        self.assertEqual(comp.last_compression["before_tokens"], 0)

    def test_stays_none_on_noop_compression(self):
        comp = compressor.RollingCompressor(keep_floor=2, keep_turns=2)
        result = comp.compress([{"role": "user", "content": "hi"}],
                               auth_headers={}, real_token_count=999,
                               payload={"model": "claude-sonnet-4-5-20250929"})
        self.assertIsNone(result)              # nothing to compress
        self.assertIsNone(comp.last_compression)


class HealthSurfacesLastCompressionTest(unittest.TestCase):
    def test_health_reports_last_compression(self):
        saved = server.compressor.last_compression
        try:
            sentinel = {"ts": 123.0, "before_chars": 900,
                        "after_chars": 300, "before_tokens": 250}
            server.compressor.last_compression = sentinel
            out = _health_json()["last_compression"]
            # /health formats ts to ISO 8601 local; other keys pass through.
            self.assertEqual(out["ts"], server._iso(123.0))
            self.assertEqual({k: out[k] for k in out if k != "ts"},
                             {k: sentinel[k] for k in sentinel if k != "ts"})
        finally:
            server.compressor.last_compression = saved

    def test_health_null_when_no_compression(self):
        saved = server.compressor.last_compression
        try:
            server.compressor.last_compression = None
            self.assertIsNone(_health_json()["last_compression"])
        finally:
            server.compressor.last_compression = saved


if __name__ == "__main__":
    unittest.main()
