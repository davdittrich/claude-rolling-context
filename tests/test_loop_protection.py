"""An inbound request already carrying our own X-Rolling-Context-Chained-From
marker is refused as a loop rather than forwarded -- catches a cycle through
an intermediate proxy, not just a direct self-chain (spec section 7, Step 5).
Uses host_matches normalization, so localhost and 127.0.0.1 are one host.

Run: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "proxy"))
import chain
import server

from _fakes import hermetic_home, make_handler, write_user_settings


class LoopProtectionTest(unittest.TestCase):
    def setUp(self):
        self.home = hermetic_home(self)
        server._UPSTREAM_CACHE.update(stamp=None, value=None)
        self.addCleanup(server._UPSTREAM_CACHE.update, stamp=None, value=None)
        # Nothing listens here: a test that isn't refused as a loop still
        # must not reach the real network -- it fails fast on connect instead.
        write_user_settings(self.home, {"ROLLING_CONTEXT_UPSTREAM": "http://127.0.0.1:59998"})

    def test_our_own_address_in_the_header_is_refused_as_a_loop(self):
        # chain.our_bind() -- not server.LISTEN_PORT, which froze at import,
        # possibly before this setUp popped ROLLING_CONTEXT_PORT -- is what
        # _loop_detected() actually compares against at call time.
        _, our_port = chain.our_bind()
        handler = make_handler(b"{}", headers={"X-Rolling-Context-Chained-From":
                                               f"http://127.0.0.1:{our_port}"})
        handler.do_POST()
        self.assertIn(b"loop", handler.wfile.getvalue().lower())

    def test_alternate_loopback_spelling_is_still_caught(self):
        _, our_port = chain.our_bind()
        handler = make_handler(b"{}", headers={"X-Rolling-Context-Chained-From":
                                               f"http://localhost:{our_port}"})
        handler.do_POST()
        self.assertIn(b"loop", handler.wfile.getvalue().lower())

    def test_a_different_chained_from_address_forwards_normally(self):
        handler = make_handler(b"{}", headers={"X-Rolling-Context-Chained-From":
                                               "http://127.0.0.1:9999"})
        handler.do_POST()
        self.assertNotIn(b"loop", handler.wfile.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
