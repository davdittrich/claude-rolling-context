"""install.sh seeds ANTHROPIC_BASE_URL in three cases and never chains silently (spec section 9).

Hermetic: HOME is a fresh tempdir and the ambient upstream-resolution variables are stripped,
so the installer sees only what each test writes.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(REPO, "install.sh")
FOREIGN = "http://127.0.0.1:8787"
AMBIENT = ("ANTHROPIC_BASE_URL", "ROLLING_CONTEXT_UPSTREAM", "ROLLING_CONTEXT_PORT",
           "ROLLING_CONTEXT_SUMMARIZER_URL")


class InstallSeedingTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="install-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)

    def _run(self):
        env = dict(os.environ, HOME=self.home, ROLLING_CONTEXT_NO_START="1")
        for key in AMBIENT:
            env.pop(key, None)
        return subprocess.run(["bash", INSTALL], env=env, capture_output=True, text=True,
                              timeout=60)

    def _user_env(self):
        path = os.path.join(self.home, ".claude", "settings.json")
        if not os.path.exists(path):
            return {}
        with open(path) as f:
            return json.load(f).get("env", {})

    def _set(self, value):
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path, "w") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": value}}, f)

    def test_absent_writes_ours(self):
        self._run()
        self.assertIn("5588", self._user_env()["ANTHROPIC_BASE_URL"])

    def test_ours_is_left_alone(self):
        self._set("http://127.0.0.1:5588")
        self._run()
        self.assertEqual(self._user_env()["ANTHROPIC_BASE_URL"], "http://127.0.0.1:5588")

    def test_foreign_writes_nothing_and_prints_guidance(self):
        self._set(FOREIGN)
        result = self._run()
        self.assertEqual(self._user_env()["ANTHROPIC_BASE_URL"], FOREIGN)
        self.assertIn("chain", result.stdout.lower())

    def test_foreign_is_never_chained_behind_our_back(self):
        # The old block wrote ROLLING_CONTEXT_UPSTREAM=<foreign> and took the variable --
        # an unrecorded chain no undo could see.
        self._set(FOREIGN)
        self._run()
        self.assertNotIn("ROLLING_CONTEXT_UPSTREAM", self._user_env())


if __name__ == "__main__":
    unittest.main()
