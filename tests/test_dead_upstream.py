"""D9: a refused or unreachable upstream renders as an Anthropic-shaped
message (HTTP 200, error.type == "api_error") so Claude Code shows the user
a message instead of treating it as a transport crash (spec section 7).

Run: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "proxy"))
import chain
import server

from _fakes import hermetic_home, make_handler, start_listener, write_user_settings


class DeadUpstreamTest(unittest.TestCase):
    def setUp(self):
        self.home = hermetic_home(self)
        server._UPSTREAM_CACHE.update(stamp=None, value=None)
        self.addCleanup(server._UPSTREAM_CACHE.update, stamp=None, value=None)

    def _point_at(self, port):
        write_user_settings(self.home, {"ROLLING_CONTEXT_UPSTREAM": f"http://127.0.0.1:{port}"})

    def test_connection_refused_returns_an_anthropic_shaped_body(self):
        self._point_at(59999)          # nothing listening
        handler = make_handler(b'{"model":"claude-opus-5","messages":[],"max_tokens":1}')
        handler.do_POST()
        body = json.loads(handler.wfile.getvalue().split(b"\r\n\r\n", 1)[1])
        self.assertIn(b"200", handler.wfile.getvalue().split(b"\r\n", 1)[0])
        self.assertEqual(body["error"]["type"], "api_error")
        self.assertIn("not answering", body["error"]["message"])

    def test_tier_one_wording_names_the_variable_not_unchain(self):
        with mock.patch.dict(os.environ, {"ROLLING_CONTEXT_UPSTREAM": "http://127.0.0.1:59999"}):
            handler = make_handler(b'{"model":"claude-opus-5","messages":[],"max_tokens":1}')
            handler.do_POST()
            body = json.loads(handler.wfile.getvalue().split(b"\r\n\r\n", 1)[1])
            self.assertIn("ROLLING_CONTEXT_UPSTREAM", body["error"]["message"])
            self.assertNotIn("chain.sh unchain", body["error"]["message"])

    def test_the_error_envelope_is_anthropic_shaped(self):
        self._point_at(59999)          # nothing listening
        handler = make_handler(b'{"model":"claude-opus-5","messages":[],"max_tokens":1}')
        handler.do_POST()
        body = json.loads(handler.wfile.getvalue().split(b"\r\n\r\n", 1)[1])
        # The full Anthropic shape, not just the inner type: Claude Code keys
        # off the top-level "type" too, so dropping it must fail a test.
        self.assertEqual(body["type"], "error")
        self.assertEqual(set(body), {"type", "error"})
        self.assertEqual(set(body["error"]), {"type", "message"})
        self.assertIsInstance(body["error"]["message"], str)

    def test_the_file_sourced_wording_names_the_unchain_command(self):
        self._point_at(59999)          # nothing listening
        handler = make_handler(b'{"model":"claude-opus-5","messages":[],"max_tokens":1}')
        handler.do_POST()
        body = json.loads(handler.wfile.getvalue().split(b"\r\n\r\n", 1)[1])
        # D9 is a recovery instruction, not just a diagnosis: assert the exact
        # runnable command, rooted at this server's own checkout.
        resolved = os.path.dirname(os.path.dirname(os.path.abspath(server.__file__)))
        self.assertIn(f"Run: bash {resolved}/hooks/chain.sh unchain", body["error"]["message"])

    def test_an_env_upstream_pointing_at_us_is_refused_as_a_loop(self):
        _, our_port = chain.our_bind()
        with mock.patch.dict(os.environ,
                             {"ROLLING_CONTEXT_UPSTREAM": f"http://127.0.0.1:{our_port}"}):
            handler = make_handler(b'{"model":"claude-opus-5","messages":[],"max_tokens":1}')
            handler.do_POST()
            body = json.loads(handler.wfile.getvalue().split(b"\r\n\r\n", 1)[1])
            self.assertEqual(body["error"]["type"], "api_error")
            self.assertIn("points back at this proxy itself", body["error"]["message"])
            self.assertIn("ROLLING_CONTEXT_UPSTREAM", body["error"]["message"])

    def test_a_live_upstream_status_passes_through_untouched(self):
        httpd = start_listener(5911)
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        self._point_at(5911)
        handler = make_handler(b'{"model":"claude-opus-5","messages":[],"max_tokens":1}')
        handler.do_POST()
        self.assertIn(b"200", handler.wfile.getvalue().split(b"\r\n", 1)[0])


if __name__ == "__main__":
    unittest.main()
