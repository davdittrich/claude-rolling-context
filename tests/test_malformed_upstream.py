"""A malformed upstream must degrade, never kill the daemon (Gemini-0p1).

current_upstream() parsed the URL unguarded, so a bad port or a malformed IPv6
literal raised ValueError out of both _handle_health and main() -- neither of
which catches it. The daemon died before binding its socket.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "proxy"))

import chain  # noqa: E402
import server  # noqa: E402

from _fakes import hermetic_home  # noqa: E402

MALFORMED = [
    "http://127.0.0.1:abc",
    "http://[::1",
    "http://127.0.0.1:99999",
]


class MalformedUpstreamTest(unittest.TestCase):
    def setUp(self):
        self.home = hermetic_home(self)
        server._UPSTREAM_CACHE["stamp"] = None
        server._UPSTREAM_CACHE["value"] = None
        self.addCleanup(lambda: server._UPSTREAM_CACHE.update({"stamp": None, "value": None}))

    def _write_upstream(self, value):
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"env": {"ROLLING_CONTEXT_UPSTREAM": value}}, f)
        server._UPSTREAM_CACHE["stamp"] = None
        server._UPSTREAM_CACHE["value"] = None

    def test_a_malformed_upstream_raises_the_typed_error_not_valueerror(self):
        for value in MALFORMED:
            with self.subTest(value=value):
                self._write_upstream(value)
                with self.assertRaises(server.UpstreamRefused) as caught:
                    server.current_upstream()
                self.assertEqual(caught.exception.reason, "malformed")

    def test_the_error_body_names_the_malformed_value(self):
        self._write_upstream("http://127.0.0.1:abc")
        try:
            server.current_upstream()
        except server.UpstreamRefused as exc:
            body = json.loads(server._upstream_error_body(exc))
        self.assertEqual(body["type"], "error")
        self.assertIn("127.0.0.1:abc", body["error"]["message"])

    def test_the_daemon_still_binds_with_a_malformed_upstream(self):
        home = tempfile.mkdtemp(prefix="malformed-daemon-")
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        with open(os.path.join(home, ".claude", "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"env": {"ROLLING_CONTEXT_UPSTREAM": "http://127.0.0.1:abc"}}, f)
        env = dict(os.environ, HOME=home, ROLLING_CONTEXT_PORT="5607")
        for key in ("ANTHROPIC_BASE_URL", "ROLLING_CONTEXT_UPSTREAM",
                    "ROLLING_CONTEXT_SUMMARIZER_URL"):
            env.pop(key, None)
        proc = subprocess.Popen([sys.executable, os.path.join(REPO, "proxy", "server.py")],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            # A daemon that degrades correctly never exits on its own -- it logs
            # a warning and calls serve_forever(). Still running after a few
            # seconds *is* the passing signal: it bound its socket instead of
            # dying. Only an early exit needs its stderr inspected.
            _, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            _, err = proc.communicate(timeout=10)
        # It must not die on a traceback. It may exit for any other reason.
        self.assertNotIn("ValueError", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
