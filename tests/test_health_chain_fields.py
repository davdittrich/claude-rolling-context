"""/health exposes `chained` and `upstream_reachable` beside the sanitized
`upstream_url`, which is re-serialized from the parsed Upstream and never
echoes a raw settings value (spec section 7, Step 4c).

Run: python3 -m unittest discover -s tests
"""
import io
import json
import os
import sys
import unittest
from email.message import Message

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "proxy"))
import server

from _fakes import hermetic_home, write_user_settings


def _health_json():
    handler = server.ProxyHandler.__new__(server.ProxyHandler)
    handler.request_version = "HTTP/1.1"
    handler.command = "GET"
    handler.path = "/health"
    handler.requestline = "GET /health HTTP/1.1"
    handler.client_address = ("127.0.0.1", 0)
    handler.headers = Message()
    handler.rfile = io.BytesIO(b"")
    handler.wfile = io.BytesIO()
    handler.do_GET()
    return json.loads(handler.wfile.getvalue().split(b"\r\n\r\n", 1)[1])


class HealthChainFieldsTest(unittest.TestCase):
    def setUp(self):
        self.home = hermetic_home(self)
        server._UPSTREAM_CACHE.update(stamp=None, value=None)
        self.addCleanup(server._UPSTREAM_CACHE.update, stamp=None, value=None)

    def _write_user_settings(self, env):
        write_user_settings(self.home, env)

    def test_health_exposes_chained_and_reachable(self):
        data = _health_json()
        self.assertIn("chained", data)
        self.assertIn("upstream_reachable", data)

    def test_health_url_is_reserialized_not_echoed(self):
        self._write_user_settings({"ROLLING_CONTEXT_UPSTREAM": "http://127.0.0.1:8787/../x"})
        self.assertNotIn("..", _health_json()["upstream_url"])


if __name__ == "__main__":
    unittest.main()
