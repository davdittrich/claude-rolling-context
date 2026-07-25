"""zd2.2: /health exposes the last 3 routed requests (before/after size + ts).

Covers the ring buffer (order, partial-fill, maxlen eviction, key set) and,
end-to-end through _handle_messages, that after_tokens is the REAL upstream
token count and never the chars//4 estimate.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeUpstreamConn, FakeUpstreamResponse, make_handler  # noqa: E402

import server  # noqa: E402


def _health_json():
    handler = make_handler(b"")
    handler._handle_health()
    body = handler.wfile.getvalue().split(b"\r\n\r\n", 1)[1]
    return json.loads(body)


class RecentRequestsRingBufferTest(unittest.TestCase):
    def setUp(self):
        server._recent_requests.clear()

    def test_partial_fill_shorter_than_max(self):
        self.assertEqual(_health_json()["recent_requests"], [])
        server._record_request(10, 10, False, 0)
        self.assertEqual(len(_health_json()["recent_requests"]), 1)
        server._record_request(20, 20, False, 0)
        self.assertEqual(len(_health_json()["recent_requests"]), 2)

    def test_newest_first_and_maxlen_eviction(self):
        for n in (1, 2, 3, 4):
            server._record_request(n, n, False, n)
        recent = _health_json()["recent_requests"]
        self.assertEqual(len(recent), 3)  # maxlen=3, oldest (1) evicted
        # newest first
        self.assertEqual([r["before_chars"] for r in recent], [4, 3, 2])

    def test_exact_key_set_and_ts_is_iso_string(self):
        import datetime
        server._record_request(100, 40, True, 25)
        rec = _health_json()["recent_requests"][0]
        self.assertEqual(set(rec), {"ts", "before_chars", "after_chars", "injected", "after_tokens"})
        # /health emits ts as an ISO 8601 local datetime string; internal record stays numeric.
        self.assertIsInstance(rec["ts"], str)
        datetime.datetime.fromisoformat(rec["ts"])  # parses or raises
        self.assertIsInstance(server._recent_snapshot()[-1]["ts"], float)
        self.assertNotIn("before_tokens", rec)


class RecentRequestsEndToEndTest(unittest.TestCase):
    def setUp(self):
        server._recent_requests.clear()

    def _drive(self, request_body, upstream_body):
        handler = make_handler(request_body)
        fake_resp = FakeUpstreamResponse(
            200, "OK",
            [("content-type", "application/json"),
             ("content-length", str(len(upstream_body)))],
            upstream_body,
        )
        with patch("server._upstream_conn", return_value=FakeUpstreamConn(fake_resp)):
            handler._handle_messages()
        return server._recent_snapshot()[-1]

    def test_records_real_after_tokens_no_injection(self):
        req = json.dumps({
            "model": "claude-x", "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()
        upstream = json.dumps({
            "id": "m", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "hello"}],
            "usage": {"input_tokens": 5000, "cache_creation_input_tokens": 100,
                      "cache_read_input_tokens": 50, "output_tokens": 10},
        }).encode()
        rec = self._drive(req, upstream)
        self.assertEqual(rec["after_tokens"], 5150)   # real upstream total
        self.assertFalse(rec["injected"])
        self.assertEqual(rec["before_chars"], rec["after_chars"])  # no injection

    def test_after_tokens_zero_never_estimate(self):
        # Usage-less body: total_input falls back to chars//4 for the trigger,
        # but the record must keep the honest real value: 0.
        req = json.dumps({
            "model": "claude-x", "stream": False,
            "messages": [{"role": "user", "content": "hello there, this needs enough chars"}],
        }).encode()
        upstream = json.dumps({
            "id": "m", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
        }).encode()
        rec = self._drive(req, upstream)
        self.assertEqual(rec["after_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
