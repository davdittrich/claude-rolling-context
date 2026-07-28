"""The upstream a REQUEST actually reaches, with no daemon restart (spec section 7).

A string-only fix leaves _parsed_upstream, UPSTREAM_PATH and the connection factory frozen, so this
test drives real sockets rather than asserting on a resolved value.

Run: python3 -m unittest discover -s tests
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

# Every test file in this repo inserts proxy/ before importing server -- see tests/_fakes.py:25.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "proxy"))

import server

A, B = 5951, 5952


def _listener(port, hits):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            hits.append(port)
            body = b'{"type":"message","role":"assistant","content":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class UpstreamReachesSocketTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="socket-")
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        patch = mock.patch.dict(os.environ, {"HOME": self.home}, clear=False)
        patch.start()
        # Hermetic: this machine may export ANTHROPIC_BASE_URL (headroom does).
        # patch.dict restores the whole mapping on stop, so these pops are undone with it.
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        os.environ.pop("ROLLING_CONTEXT_UPSTREAM", None)
        os.environ.pop("ROLLING_CONTEXT_PORT", None)
        self.addCleanup(patch.stop)
        self.hits = []
        self.servers = [_listener(A, self.hits), _listener(B, self.hits)]
        for s in self.servers:
            self.addCleanup(s.server_close)
            self.addCleanup(s.shutdown)

    def _point_at(self, port):
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"env": {"ROLLING_CONTEXT_UPSTREAM": f"http://127.0.0.1:{port}"}}, f)

    def _one_request(self):
        """Drive the real request path, not a parallel one built for the test.

        tests/_fakes.py::make_handler is what the existing suite uses to invoke ProxyHandler
        against a captured socket; reuse it so this test exercises the same code a live request does.
        """
        from _fakes import make_handler
        body = json.dumps({"model": "claude-opus-5", "max_tokens": 1,
                           "messages": [{"role": "user", "content": "hi"}]}).encode()
        handler = make_handler(body)
        handler.path = "/v1/messages"
        handler.do_POST()

    def test_second_request_follows_a_changed_upstream_without_restart(self):
        self._point_at(A)
        self._one_request()
        self._point_at(B)
        self._one_request()
        self.assertEqual(self.hits, [A, B])

    def test_connection_factory_uses_the_live_value(self):
        self._point_at(B)
        up = server.current_upstream()
        self.assertEqual((up.host, up.port), ("127.0.0.1", B))


if __name__ == "__main__":
    unittest.main()
