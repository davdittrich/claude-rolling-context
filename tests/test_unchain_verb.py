"""unchain: give back what we took; our own key is deleted, not restored (spec section 5, 6).

Run: python3 -m unittest discover -s tests
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from proxy import chain

FOREIGN = "http://127.0.0.1:8787"


class UnchainTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="unchain-home-")
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        patch = mock.patch.dict(os.environ, {"HOME": self.home}, clear=False)
        patch.start()
        # Hermetic: this machine may export ANTHROPIC_BASE_URL (headroom does).
        # patch.dict restores the whole mapping on stop, so these pops are undone with it.
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        os.environ.pop("ROLLING_CONTEXT_UPSTREAM", None)
        os.environ.pop("ROLLING_CONTEXT_PORT", None)
        self.addCleanup(patch.stop)

    def _project(self, name):
        root = tempfile.mkdtemp(prefix=f"unchain-{name}-")
        os.makedirs(os.path.join(root, ".claude"), exist_ok=True)
        with open(os.path.join(root, ".claude", "settings.local.json"), "w", encoding="utf-8") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": FOREIGN}}, f)
        return root

    def _user_env(self):
        path = os.path.join(self.home, ".claude", "settings.json")
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("env", {})

    def _local_env(self, root):
        with open(os.path.join(root, ".claude", "settings.local.json"), encoding="utf-8") as f:
            return json.load(f).get("env", {})

    def test_restores_the_displaced_base_url(self):
        a = self._project("a")
        chain.do_chain(a, assume_yes=True)
        self.assertEqual(chain.do_unchain(a), 0)
        self.assertEqual(self._local_env(a)["ANTHROPIC_BASE_URL"], FOREIGN)

    def test_all_deletes_our_own_key(self):
        a = self._project("a")
        chain.do_chain(a, assume_yes=True)
        chain.do_unchain(a, all_=True)
        self.assertNotIn("ROLLING_CONTEXT_UPSTREAM", self._user_env())

    def test_plain_unchain_leaves_our_own_key_set(self):
        # It is inert for this project once ANTHROPIC_BASE_URL is restored, and a second
        # project may still be chained through it. Only --all removes it (D10).
        a, b = self._project("a"), self._project("b")
        chain.do_chain(a, assume_yes=True)
        chain.do_chain(b, assume_yes=True)
        chain.do_unchain(a)
        self.assertEqual(self._user_env()["ROLLING_CONTEXT_UPSTREAM"], FOREIGN)

    def test_a_second_project_still_resolves_after_the_first_unchains(self):
        a, b = self._project("a"), self._project("b")
        chain.do_chain(a, assume_yes=True)
        chain.do_chain(b, assume_yes=True)
        chain.do_unchain(a)
        self.assertTrue(chain.is_self(self._local_env(b)["ANTHROPIC_BASE_URL"]))
        self.assertEqual(self._user_env()["ROLLING_CONTEXT_UPSTREAM"], FOREIGN)

    def test_deleted_base_url_is_skipped_not_resurrected(self):
        # headroom removes this key when it exits (wrap.py:1779-1781).
        a = self._project("a")
        chain.do_chain(a, assume_yes=True)
        with open(os.path.join(a, ".claude", "settings.local.json"), "w", encoding="utf-8") as f:
            json.dump({"env": {}}, f)
        self.assertEqual(chain.do_unchain(a), 0)
        self.assertNotIn("ANTHROPIC_BASE_URL", self._local_env(a))

    def test_all_skips_when_the_key_is_no_longer_ours(self):
        a = self._project("a")
        chain.do_chain(a, assume_yes=True)
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"env": {"ROLLING_CONTEXT_UPSTREAM": "http://127.0.0.1:9999"}}, f)
        self.assertEqual(chain.do_unchain(a, all_=True), 0)
        self.assertEqual(self._user_env()["ROLLING_CONTEXT_UPSTREAM"], "http://127.0.0.1:9999")

    def test_no_project_ancestor_is_an_exit_zero_report(self):
        self.assertEqual(chain.do_unchain(self.home), 0)

    def test_no_record_says_so_plainly_not_unchained(self):
        # D2/M5: a no-op unchain (nothing recorded for this project) must not print
        # "unchained" -- that's indistinguishable from a real restore.
        a = self._project("a")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = chain.do_unchain(a)
        self.assertEqual(code, 0)
        self.assertIn("nothing recorded", buf.getvalue())
        self.assertNotIn("unchained", buf.getvalue())

    def test_a_project_reached_by_symlink_still_unchains(self):
        # do_chain keys its state record with os.path.realpath(project); do_unchain
        # looks that key up via project_root(), which also realpath()s its start.
        # tempfile.mkdtemp() already returns a real path on this machine, so a
        # project reached directly would pass even with the realpath() call
        # removed. Route through a symlink so the normalisation is actually
        # exercised, matching e.g. macOS's TMPDIR sitting under /var -> /private/var.
        real = tempfile.mkdtemp(prefix="unchain-real-")
        os.makedirs(os.path.join(real, ".claude"), exist_ok=True)
        with open(os.path.join(real, ".claude", "settings.local.json"), "w", encoding="utf-8") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": FOREIGN}}, f)
        link = os.path.join(tempfile.mkdtemp(prefix="unchain-link-"), "alias")
        os.symlink(real, link)

        self.assertEqual(chain.do_chain(link, assume_yes=True), 0)
        self.assertEqual(chain.do_unchain(real), 0)
        self.assertEqual(self._local_env(real)["ANTHROPIC_BASE_URL"], FOREIGN)

    def test_plain_unchain_drops_our_key_once_no_project_is_chained(self):
        """The shared key outlives a single unchain only while someone still owns it.

        Left set with no records, it silently routes the NEXT project seeded to our port
        through a proxy that project never asked for.
        """
        a = self._project("a")
        chain.do_chain(a, assume_yes=True)
        self.assertEqual(self._user_env()["ROLLING_CONTEXT_UPSTREAM"], FOREIGN)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(chain.do_unchain(a), 0)
        self.assertNotIn("ROLLING_CONTEXT_UPSTREAM", self._user_env())
        with open(os.path.join(a, ".claude", "settings.local.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["env"]["ANTHROPIC_BASE_URL"], FOREIGN)


if __name__ == "__main__":
    unittest.main()
