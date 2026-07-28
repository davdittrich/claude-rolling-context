"""compressor.summarizer_endpoint() follows the live upstream, unless
ROLLING_CONTEXT_SUMMARIZER_URL is set -- a frozen summarizer URL would send
compaction traffic to the old upstream while chat requests follow the new
one (spec section 7, Step 4).

Run: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "proxy"))
import compressor
import server

from _fakes import hermetic_home, write_user_settings


class SummarizerFollowsTest(unittest.TestCase):
    def setUp(self):
        self.home = hermetic_home(self)
        server._UPSTREAM_CACHE.update(stamp=None, value=None)
        self.addCleanup(server._UPSTREAM_CACHE.update, stamp=None, value=None)

    def _point_at(self, port):
        write_user_settings(self.home, {"ROLLING_CONTEXT_UPSTREAM": f"http://127.0.0.1:{port}"})

    def test_summarizer_follows_a_changed_upstream(self):
        self._point_at(5921)
        self.assertEqual(compressor.summarizer_endpoint().port, 5921)
        self._point_at(5922)
        self.assertEqual(compressor.summarizer_endpoint().port, 5922)

    def test_explicit_override_still_wins(self):
        with mock.patch.dict(os.environ,
                             {"ROLLING_CONTEXT_SUMMARIZER_URL": "http://127.0.0.1:7777"}):
            self._point_at(5922)
            self.assertEqual(compressor.summarizer_endpoint().port, 7777)


if __name__ == "__main__":
    unittest.main()
