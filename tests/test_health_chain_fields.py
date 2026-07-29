"""/health exposes `chained` and `upstream_reachable` beside the sanitized
`upstream_url`, which is re-serialized from the parsed Upstream and never
echoes a raw settings value (spec section 7, Step 4c).

Run: python3 -m unittest discover -s tests
"""
import io
import json
import os
import sys
import tempfile
import unittest
from email.message import Message
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "proxy"))
import chain
import server

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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

    def test_health_reports_where_the_upstream_came_from(self):
        self._write_user_settings({"ROLLING_CONTEXT_UPSTREAM": "http://127.0.0.1:8787"})
        self.assertEqual(_health_json()["upstream_source"], chain.user_settings_path())

    def test_health_source_names_the_environment_when_the_env_var_wins(self):
        with mock.patch.dict(os.environ, {"ROLLING_CONTEXT_UPSTREAM": "http://127.0.0.1:8787"}):
            self.assertEqual(_health_json()["upstream_source"], "<environment>")

    def test_health_source_escapes_control_bytes_in_the_path(self):
        # Same sanitizing discipline the URL gets: the source path is echoed to
        # a terminal by `status`, so control bytes must not survive.
        home = tempfile.mkdtemp(prefix="rolling-context-home-\x1b[31m")
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        patch = mock.patch.dict(os.environ, {"HOME": home}, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        write_user_settings(home, {"ROLLING_CONTEXT_UPSTREAM": "http://127.0.0.1:8787"})
        server._UPSTREAM_CACHE.update(stamp=None, value=None)
        self.assertNotIn("\x1b", _health_json()["upstream_source"])

    def test_health_degrades_when_the_settings_file_is_unparseable(self):
        # /health is the visibility surface this plan exists for: a hand-broken
        # settings.json must read as a diagnostic, never a traceback.
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        data = _health_json()
        self.assertEqual(data["status"], "ok")
        self.assertFalse(data["upstream_reachable"])
        self.assertIn("not valid JSON", data["upstream_url"])
        self.assertEqual(data["upstream_source"], path)
        # Never claim a chain we just said we cannot read.
        self.assertFalse(data["chained"])

    def test_health_reports_the_refusal_when_the_upstream_is_refused(self):
        # A D18 refusal is what the `status` verb renders; all three fields of
        # that arm are load-bearing, so pin each one.
        self._write_user_settings({"ROLLING_CONTEXT_UPSTREAM": "https://proxy.example.com"})
        data = _health_json()
        self.assertEqual(data["upstream_url"], "(refused: not-loopback)")
        self.assertEqual(data["upstream_source"], chain.user_settings_path())
        self.assertTrue(data["chained"])
        self.assertFalse(data["upstream_reachable"])

    def test_chained_is_false_for_the_default_api_and_true_for_a_local_upstream(self):
        """Both directions in one assertion: checking only one side lets the
        `!=` polarity in _handle_health flip to `==` and still pass."""
        self.assertFalse(_health_json()["chained"])
        self._write_user_settings({"ROLLING_CONTEXT_UPSTREAM": "http://127.0.0.1:8787"})
        self.assertTrue(_health_json()["chained"])


if __name__ == "__main__":
    unittest.main()
