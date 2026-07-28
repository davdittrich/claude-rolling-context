"""ROLLING_CONTEXT_UPSTREAM moving underneath us is its own failure mode (spec section 8).

ANTHROPIC_BASE_URL still points at us, so the displacement check sees nothing changed -- but
we are now chaining somewhere the user did not choose, or are silently un-chained.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO, "hooks", "start-proxy.sh")
CHAIN = os.path.join(REPO, "proxy", "chain.py")
FOREIGN = "http://127.0.0.1:8787"
AMBIENT = ("ANTHROPIC_BASE_URL", "ROLLING_CONTEXT_UPSTREAM", "ROLLING_CONTEXT_PORT",
           "ROLLING_CONTEXT_SUMMARIZER_URL")


class UpstreamDriftTest(unittest.TestCase):
    def setUp(self):
        self.home = self._tempdir("drift-home-")
        self.project = self._tempdir("drift-proj-")
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        os.makedirs(os.path.join(self.project, ".claude"), exist_ok=True)

    def _tempdir(self, prefix):
        path = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path

    def _env(self):
        env = dict(os.environ, HOME=self.home, ROLLING_CONTEXT_NO_START="1")
        for key in AMBIENT:
            env.pop(key, None)
        return env

    def _chain_through(self, url):
        with open(os.path.join(self.project, ".claude", "settings.local.json"), "w") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": url}}, f)
        result = subprocess.run([sys.executable, CHAIN, "chain", "--yes"], cwd=self.project,
                                env=self._env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def _set_user_upstream(self, url):
        """Read-modify-write, exactly as a foreign tool rewriting settings.json would."""
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path) as f:
            data = json.load(f)
        data.setdefault("env", {})["ROLLING_CONTEXT_UPSTREAM"] = url
        with open(path, "w") as f:
            json.dump(data, f)

    def _hook_stdout(self):
        return subprocess.run(["bash", HOOK], cwd=self.project, env=self._env(),
                              capture_output=True, text=True, timeout=30).stdout

    def test_drift_alerts_while_we_are_still_in_the_path(self):
        self._chain_through(FOREIGN)
        self._set_user_upstream("http://127.0.0.1:9999")
        self.assertIn("changed outside", self._hook_stdout())

    def test_no_drift_alert_when_the_value_is_still_ours(self):
        self._chain_through(FOREIGN)
        self.assertEqual(self._hook_stdout().strip(), "")

    def test_a_second_unrelated_drift_is_not_suppressed(self):
        self._chain_through(FOREIGN)
        self._set_user_upstream("http://127.0.0.1:9999")
        self._hook_stdout()
        self._set_user_upstream("http://127.0.0.1:7777")
        self.assertIn("changed outside", self._hook_stdout())

    def test_the_same_drift_is_not_repeated_every_session(self):
        self._chain_through(FOREIGN)
        self._set_user_upstream("http://127.0.0.1:9999")
        self.assertIn("changed outside", self._hook_stdout())
        self.assertEqual(self._hook_stdout().strip(), "")

    def test_the_drift_alert_is_one_json_object_with_both_fields(self):
        self._chain_through(FOREIGN)
        self._set_user_upstream("http://127.0.0.1:9999")
        payload = json.loads(self._hook_stdout().strip())
        self.assertIn("systemMessage", payload)
        self.assertIn("9999", payload["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main()
