"""current_upstream(): precedence, D18 loopback guard, mtime-based cache
invalidation, and self-pointer fallthrough (spec section 7, D18).

Run: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "proxy"))
import chain
import server

from _fakes import hermetic_home, write_user_settings


class ServerUpstreamTest(unittest.TestCase):
    def setUp(self):
        self.home = hermetic_home(self)
        server._UPSTREAM_CACHE.update(stamp=None, value=None)
        self.addCleanup(server._UPSTREAM_CACHE.update, stamp=None, value=None)

    def _point_at(self, port):
        write_user_settings(self.home, {"ROLLING_CONTEXT_UPSTREAM": f"http://127.0.0.1:{port}"})

    def _write_user_settings(self, env):
        write_user_settings(self.home, env)

    def test_the_same_value_at_tier_one_is_honoured(self):
        with mock.patch.dict(os.environ,
                             {"ROLLING_CONTEXT_UPSTREAM": "https://proxy.example.com"}):
            self.assertEqual(server.current_upstream().host, "proxy.example.com")

    def test_cache_invalidates_on_mtime_change(self):
        self._point_at(5001)
        self.assertEqual(server.current_upstream().port, 5001)
        self._point_at(5002)
        self.assertEqual(server.current_upstream().port, 5002)

    def test_a_malformed_file_sourced_value_is_refused_not_forwarded(self):
        self._write_user_settings({"ROLLING_CONTEXT_UPSTREAM": "not-a-url"})
        with self.assertRaises(server.UpstreamRefused):
            server.current_upstream()

    def test_our_own_address_falls_through_to_the_default_api(self):
        # chain.our_bind() -- not server.LISTEN_PORT, which froze at import,
        # possibly before this setUp popped ROLLING_CONTEXT_PORT -- is what
        # chain.is_self() actually compares against at call time.
        _, our_port = chain.our_bind()
        self._write_user_settings(
            {"ROLLING_CONTEXT_UPSTREAM": f"http://127.0.0.1:{our_port}"})
        self.assertEqual(server.current_upstream().host, "api.anthropic.com")

    def test_non_loopback_at_tier_two_is_refused_and_names_the_file(self):
        self._write_user_settings({"ROLLING_CONTEXT_UPSTREAM": "https://proxy.example.com"})
        with self.assertRaises(server.UpstreamRefused) as ctx:
            server.current_upstream()
        self.assertEqual(ctx.exception.path, chain.user_settings_path())

    def test_an_unchained_proxy_reports_the_default_source(self):
        # Commonest runtime value of the field: nothing in env, nothing in the
        # settings file, so the upstream came from neither.
        self.assertEqual(server.current_upstream().source, "(default)")

    def test_a_foreign_loopback_base_url_is_used_not_discarded_as_self(self):
        """A foreign (non-self) ANTHROPIC_BASE_URL loopback candidate must be
        used, not silently dropped as though it pointed at us.

        The return value alone cannot pin this: an unconditional recheck a few
        lines below (`if from_file and chain.is_self(raw):`) re-evaluates
        self-ness on whatever `raw` ends up being, so a foreign candidate
        resolves identically whether the explicit
        `if candidate and not chain.is_self(candidate):` guard runs or is
        forced to `if candidate and True:` -- confirmed by direct execution
        against both versions of proxy/server.py. What differs is call
        volume: the real guard consults chain.is_self twice for a foreign
        candidate (once explicitly, once via the downstream recheck); an
        always-true guard only ever reaches the downstream recheck, once.
        Spying on chain.is_self is the only lever that observes the flip.
        """
        self._write_user_settings({"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"})
        with mock.patch.object(chain, "is_self", wraps=chain.is_self) as spy:
            up = server.current_upstream()
        self.assertEqual(up.port, 8787)
        self.assertEqual(up.host, "127.0.0.1")
        self.assertEqual(spy.call_count, 2)


if __name__ == "__main__":
    unittest.main()
