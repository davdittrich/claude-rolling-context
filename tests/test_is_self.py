"""is-self: the single predicate the seven call sites collapse into (spec section 6, section 9).

Run: python3 -m unittest discover -s tests
"""
import os
import unittest
from unittest import mock

from proxy import chain


class IsSelfTest(unittest.TestCase):
    def setUp(self):
        # Hermetic: this machine may export ROLLING_CONTEXT_PORT. Without this the suite
        # passes only when that value happens to equal the default it asserts against.
        patch = mock.patch.dict(os.environ, {}, clear=False)
        patch.start()
        os.environ.pop("ROLLING_CONTEXT_PORT", None)
        self.addCleanup(patch.stop)

    def test_our_own_url_is_self(self):
        self.assertTrue(chain.is_self("http://127.0.0.1:5588"))

    def test_loopback_spellings_are_equivalent(self):
        for host in ("127.0.0.1", "localhost", "[::1]"):
            with self.subTest(host=host):
                self.assertTrue(chain.is_self(f"http://{host}:5588"))

    def test_headroom_on_8787_is_not_self(self):
        # The original defect: any loopback address was treated as us.
        self.assertFalse(chain.is_self("http://127.0.0.1:8787"))

    def test_same_port_different_host_is_not_self(self):
        # is-self classifies; the chain guard decides whether to refuse (section 6).
        self.assertFalse(chain.is_self("http://192.168.1.10:5588"))

    def test_non_default_port_still_self_detects(self):
        with mock.patch.dict(os.environ, {"ROLLING_CONTEXT_PORT": "6001"}):
            self.assertTrue(chain.is_self("http://127.0.0.1:6001"))
            self.assertFalse(chain.is_self("http://127.0.0.1:5588"))

    def test_scheme_default_port_applies_when_absent(self):
        with mock.patch.dict(os.environ, {"ROLLING_CONTEXT_PORT": "80"}):
            self.assertTrue(chain.is_self("http://127.0.0.1"))

    def test_non_http_scheme_is_not_self(self):
        self.assertFalse(chain.is_self("ftp://127.0.0.1:5588"))

    def test_garbage_is_not_self_and_does_not_raise(self):
        for bad in ("", "not a url", "http://", ":::"):
            with self.subTest(bad=bad):
                self.assertFalse(chain.is_self(bad))

    def test_unparseable_urls_are_not_us(self):
        """Both parse-failure arms; \r and \n do NOT reach them (Python's
        urlparse() strips them), so these use inputs that genuinely fail
        to parse.
        """
        for value in (
            "http://[::1",             # urlparse() raises: Invalid IPv6 URL
            "http://127.0.0.1:abc",    # .port raises: not an integer
            "http://127.0.0.1:99999",  # .port raises: out of range
            "http://127.0.0.1: 5588",  # .port raises: not an integer
        ):
            with self.subTest(value=value):
                self.assertFalse(chain.is_self(value))


if __name__ == "__main__":
    unittest.main()
