"""chain: guards refuse without writing; apply writes upstream first, then base URL.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from proxy import chain

FOREIGN = "http://127.0.0.1:8787"


class ChainVerbTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="chain-home-")
        self.project = tempfile.mkdtemp(prefix="chain-proj-")
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        os.makedirs(os.path.join(self.project, ".claude"), exist_ok=True)
        patch = mock.patch.dict(os.environ, {"HOME": self.home}, clear=False)
        patch.start()
        # Hermetic: this machine may export ANTHROPIC_BASE_URL (headroom does).
        # patch.dict restores the whole mapping on stop, so these pops are undone with it.
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        os.environ.pop("ROLLING_CONTEXT_UPSTREAM", None)
        os.environ.pop("ROLLING_CONTEXT_PORT", None)
        self.addCleanup(patch.stop)
        self.local = os.path.join(self.project, ".claude", "settings.local.json")

    def _displace(self, url=FOREIGN):
        with open(self.local, "w", encoding="utf-8") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": url}}, f)

    def _user_env(self):
        path = os.path.join(self.home, ".claude", "settings.json")
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("env", {})

    def _local_env(self):
        with open(self.local, encoding="utf-8") as f:
            return json.load(f).get("env", {})

    def test_chain_writes_both_keys_upstream_first(self):
        self._displace()
        calls = []
        real_write_key = chain._write_key

        def recording(path, key, value):
            calls.append((key, value))
            return real_write_key(path, key, value)

        with mock.patch.object(chain, "_write_key", side_effect=recording):
            self.assertEqual(chain.do_chain(self.project, assume_yes=True), 0)
        keys_written = [key for key, _ in calls]
        self.assertLess(keys_written.index(chain.USER_KEY), keys_written.index(chain.ABU_KEY),
                         "ROLLING_CONTEXT_UPSTREAM must land before ANTHROPIC_BASE_URL")
        self.assertEqual(self._user_env()["ROLLING_CONTEXT_UPSTREAM"], FOREIGN)
        self.assertTrue(chain.is_self(self._local_env()["ANTHROPIC_BASE_URL"]))

    def test_chain_records_what_it_displaced(self):
        self._displace()
        chain.do_chain(self.project, assume_yes=True)
        state = chain.load_state()
        self.assertEqual(state["abu"][self.project]["displaced"], FOREIGN)
        self.assertEqual(state["abu"][self.project]["path"], self.local)
        self.assertEqual(state["upstream"]["wrote"], FOREIGN)
        self.assertNotIn("displaced", state["upstream"])

    def test_nothing_to_chain_is_an_exit_zero_noop(self):
        self.assertEqual(chain.do_chain(self.project, assume_yes=True), 0)
        self.assertEqual(chain.load_state(), chain.empty_state())

    def test_already_ours_is_an_exit_zero_noop(self):
        self._displace("http://127.0.0.1:5588")
        self.assertEqual(chain.do_chain(self.project, assume_yes=True), 0)

    def test_non_loopback_is_refused_and_writes_nothing(self):
        self._displace("https://proxy.example.com")
        self.assertEqual(chain.do_chain(self.project, assume_yes=True), 2)
        self.assertEqual(chain.load_state(), chain.empty_state())

    def test_declined_confirmation_writes_nothing(self):
        self._displace()
        with mock.patch("builtins.input", return_value="n"):
            self.assertEqual(chain.do_chain(self.project, assume_yes=False), 2)
        self.assertEqual(chain.load_state(), chain.empty_state())
        self.assertEqual(self._local_env()["ANTHROPIC_BASE_URL"], FOREIGN)

    def test_non_interactive_without_yes_refuses_rather_than_hanging(self):
        self._displace()
        with mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(chain.do_chain(self.project, assume_yes=False), 2)

    def test_divergent_chain_is_refused(self):
        self._displace()
        chain.do_chain(self.project, assume_yes=True)
        self._displace("http://127.0.0.1:9999")
        self.assertEqual(chain.do_chain(self.project, assume_yes=True), 2)

    def test_env_pinned_upstream_is_refused(self):
        self._displace()
        with mock.patch.dict(os.environ, {"ROLLING_CONTEXT_UPSTREAM": FOREIGN}):
            self.assertEqual(chain.do_chain(self.project, assume_yes=True), 2)

    def test_effective_abu_prints_the_winning_value_and_nothing_else(self):
        # The hook consumes this as $(chain.py effective-abu). Extra output breaks it.
        self._displace()
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch("os.getcwd", return_value=self.project):
            rc = chain.main(["effective-abu"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), FOREIGN)

    def test_matching_url_second_chain_succeeds_and_appends_a_ref(self):
        # D10: a second project through the same proxy needs the same upstream.
        self._displace()
        chain.do_chain(self.project, assume_yes=True)
        other = tempfile.mkdtemp(prefix="chain-proj2-")
        os.makedirs(os.path.join(other, ".claude"), exist_ok=True)
        with open(os.path.join(other, ".claude", "settings.local.json"), "w") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": FOREIGN}}, f)
        self.assertEqual(chain.do_chain(other, assume_yes=True), 0)
        self.assertEqual(chain.load_state()["upstream"]["wrote"], FOREIGN)

    def test_env_override_without_displacement_is_a_noop_not_a_refusal(self):
        # A user with a legitimate ROLLING_CONTEXT_UPSTREAM and nothing displacing them
        # should be told there is nothing to chain, not told to unset their variable.
        with mock.patch.dict(os.environ, {"ROLLING_CONTEXT_UPSTREAM": FOREIGN}):
            self.assertEqual(chain.do_chain(self.project, assume_yes=True), 0)

    def test_a_higher_scope_that_outranks_our_write_undoes_it(self):
        # A managed policy can arrive by a channel no file check sees. The effective-value
        # read-back is what catches it, so this asserts the undo, not the detection.
        self._displace()
        higher = os.path.join(self.project, ".claude", "settings.local.json")
        real_effective = chain.effective

        def outranked(key, root):
            if key == chain.ABU_KEY:
                return FOREIGN, higher
            return real_effective(key, root)

        with mock.patch.object(chain, "effective", side_effect=outranked):
            self.assertEqual(chain.do_chain(self.project, assume_yes=True), 1)
        self.assertEqual(chain.load_state(), chain.empty_state())
        with open(higher, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["env"]["ANTHROPIC_BASE_URL"], FOREIGN)

    def test_unparseable_settings_refuses_and_leaves_the_file(self):
        with open(self.local, "w", encoding="utf-8") as f:
            f.write("{ broken")
        self.assertEqual(chain.do_chain(self.project, assume_yes=True), 2)
        with open(self.local, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{ broken")

    def test_refuses_when_upstream_key_is_already_set_by_someone_else(self):
        # I3: a pre-existing ROLLING_CONTEXT_UPSTREAM we never wrote is foreign state --
        # chain must refuse rather than silently clobber it (and unchain --all would
        # otherwise delete it later, compounding the damage).
        user_settings = os.path.join(self.home, ".claude", "settings.json")
        existing = "http://127.0.0.1:6000"
        with open(user_settings, "w", encoding="utf-8") as f:
            json.dump({"env": {"ROLLING_CONTEXT_UPSTREAM": existing}}, f)
        self._displace()
        before_user = open(user_settings, "rb").read()
        before_local = open(self.local, "rb").read()
        state_path = chain.state_path()
        self.assertFalse(os.path.exists(state_path))

        self.assertEqual(chain.do_chain(self.project, assume_yes=True), 2)

        self.assertEqual(open(user_settings, "rb").read(), before_user)
        self.assertEqual(open(self.local, "rb").read(), before_local)
        self.assertFalse(os.path.exists(state_path))

    def test_chains_when_the_displacement_is_env_only(self):
        # D1: ANTHROPIC_BASE_URL set only in the process environment, no settings-file
        # entry anywhere -- effective() returns source="<environment>", a sentinel, not
        # a path. do_chain must write into the project's own file instead of handing
        # that sentinel to _write_key.
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": FOREIGN}):
            self.assertEqual(chain.do_chain(self.project, assume_yes=True), 0)
        self.assertTrue(chain.is_self(self._local_env()["ANTHROPIC_BASE_URL"]))
        record = chain.load_state()["abu"][self.project]
        self.assertEqual(record["path"], self.local)
        self.assertIsNone(record["displaced"])

    def test_unchain_after_env_only_chain_deletes_rather_than_restores(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": FOREIGN}):
            self.assertEqual(chain.do_chain(self.project, assume_yes=True), 0)
        self.assertEqual(chain.do_unchain(self.project), 0)
        self.assertNotIn("ANTHROPIC_BASE_URL", self._local_env())

    def test_the_environment_sentinel_never_reaches_write_key(self):
        calls = []
        real_write_key = chain._write_key

        def recording(path, key, value):
            calls.append(path)
            return real_write_key(path, key, value)

        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": FOREIGN}), \
                mock.patch.object(chain, "_write_key", side_effect=recording):
            self.assertEqual(chain.do_chain(self.project, assume_yes=True), 0)
        self.assertTrue(calls, "expected _write_key to be called")
        self.assertFalse(any("<environment>" in p for p in calls))

    def test_unknown_chain_flag_is_rejected_not_silently_ignored(self):
        self.assertEqual(chain.main(["chain", "--yse"]), 1)

    def test_unknown_unchain_flag_is_rejected_not_silently_ignored(self):
        self.assertEqual(chain.main(["unchain", "--al"]), 1)


if __name__ == "__main__":
    unittest.main()
