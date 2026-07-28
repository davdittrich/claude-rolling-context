"""main() must bind and serve even when ~/.claude/settings.json is corrupt.

Round 1 taught /health to degrade on chain.UnparseableSettings; main() still
died on it, so a hand-broken settings file meant no daemon at all -- the socket
was never bound. This drives the real startup path and asserts it survives.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import socket
import sys
import threading
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "proxy"))

import server  # noqa: E402
from _fakes import hermetic_home  # noqa: E402


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class StartupSurvivesBadSettingsTest(unittest.TestCase):
    def setUp(self):
        self.home = hermetic_home(self)
        server._UPSTREAM_CACHE.update(stamp=None, value=None)
        self.addCleanup(server._UPSTREAM_CACHE.update, stamp=None, value=None)
        self.settings = os.path.join(self.home, ".claude", "settings.json")
        with open(self.settings, "w", encoding="utf-8") as f:
            f.write("{ this is not json")

    def _serve(self):
        """Run main() on a free port; return that port once the socket is bound."""
        port = _free_port()
        bound = threading.Event()
        holder = {}
        real = server.ThreadedHTTPServer

        def capture(*args, **kwargs):
            srv = real(*args, **kwargs)  # constructor binds and listens
            holder["srv"] = srv
            bound.set()
            return srv

        def run():
            try:
                server.main()
            except BaseException as exc:  # noqa: BLE001 -- recorded, asserted below
                holder["error"] = exc

        with mock.patch.object(server, "LISTEN_PORT", port), \
                mock.patch.object(server, "ThreadedHTTPServer", capture):
            threading.Thread(target=run, daemon=True).start()
            bound.wait(10)
        self.assertIsNone(holder.get("error"),
                          f"main() died at startup: {holder.get('error')!r}")
        self.assertTrue(bound.is_set(), "main() never bound the listening socket")
        self.addCleanup(holder["srv"].server_close)
        self.addCleanup(holder["srv"].shutdown)
        return port

    def test_daemon_binds_and_serves_despite_an_unparseable_settings_file(self):
        port = self._serve()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read())
        # Degrades exactly like /health does: names the offending file, no traceback.
        self.assertEqual(data["upstream_source"], self.settings)


if __name__ == "__main__":
    unittest.main()
