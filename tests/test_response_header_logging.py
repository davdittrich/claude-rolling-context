"""Response-side header logging is name-only, matching the request side --
a chained upstream is attacker-influenceable in a way api.anthropic.com is
not, so logging response header *values* at DEBUG is a log-injection/bloat
risk (spec section 7, Step 5).

Run: python3 -m unittest discover -s tests
"""
import logging
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "proxy"))
import server

from _fakes import hermetic_home, make_handler, write_user_settings

SECRET_VALUE = "do-not-log-me-4f8a1c"


def _listener_with_marker_header(port):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = b'{"type":"message","role":"assistant","content":[]}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.send_header("X-Secret-Marker", SECRET_VALUE)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class ResponseHeaderLoggingTest(unittest.TestCase):
    def setUp(self):
        self.home = hermetic_home(self)
        server._UPSTREAM_CACHE.update(stamp=None, value=None)
        self.addCleanup(server._UPSTREAM_CACHE.update, stamp=None, value=None)
        httpd = _listener_with_marker_header(5931)
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        write_user_settings(self.home, {"ROLLING_CONTEXT_UPSTREAM": "http://127.0.0.1:5931"})

    def test_response_header_names_logged_but_not_values(self):
        handler = make_handler(b'{"model":"claude-opus-5","messages":[],"max_tokens":1}')
        with self.assertLogs("rolling-context", level="DEBUG") as captured:
            handler.do_POST()
        log_text = "\n".join(captured.output)
        self.assertIn("X-Secret-Marker", log_text)
        self.assertNotIn(SECRET_VALUE, log_text)


if __name__ == "__main__":
    unittest.main()
