"""uninstall.sh must undo the chain before it deletes the tool that undoes it (Task 10).

Before this fix, uninstall.sh:42-51 removed the plugin directory -- which holds
hooks/chain.sh -- before the settings-cleanup block at :89-127 ran, and that block only
ever read $CLAUDE_DIR/settings.json, never a project's settings.local.json. Net effect:
chain, then uninstall, left Claude Code pointing at the now-dead proxy port with no API
connectivity at all.

To make the ordering bug (and its fix) observable, these tests run uninstall.sh from a
copy planted at the exact path the script's own MARKETPLACE_DIR variable names, so the
plugin-directory-removal block actually deletes the hooks/chain.sh the running script
would otherwise need.

Hermetic: HOME is a fresh tempdir; ambient upstream-resolution variables the host machine
may export are stripped from the child environment (mirrors test_hook_output.py).

SAFETY: uninstall.sh is destructive (rm -rf under $HOME/.claude). Every subprocess below
runs with HOME pointed at tempfile.mkdtemp() -- never the real HOME.

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
UNINSTALL = os.path.join(REPO, "uninstall.sh")
CHAIN = os.path.join(REPO, "proxy", "chain.py")
BASH = shutil.which("bash") or "/usr/bin/bash"
FOREIGN = "http://127.0.0.1:8787"
AMBIENT = ("ANTHROPIC_BASE_URL", "ROLLING_CONTEXT_UPSTREAM", "ROLLING_CONTEXT_PORT",
           "ROLLING_CONTEXT_SUMMARIZER_URL")


class UninstallTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="uninstall-home-")
        self.project = tempfile.mkdtemp(prefix="uninstall-proj-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.project, ignore_errors=True)
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        os.makedirs(os.path.join(self.project, ".claude"), exist_ok=True)
        self.local = os.path.join(self.project, ".claude", "settings.local.json")
        with open(self.local, "w", encoding="utf-8") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": FOREIGN}}, f)
        # Plant a copy of uninstall.sh/chain.sh/chain.py at the exact path uninstall.sh's
        # own $MARKETPLACE_DIR names, so the plugin-directory-removal block deletes the
        # chain.sh this run would otherwise need -- the real-world shape of the bug.
        self.marketplace_dir = os.path.join(
            self.home, ".claude", "plugins", "marketplaces", "rolling-context-marketplace")
        os.makedirs(os.path.join(self.marketplace_dir, "hooks"), exist_ok=True)
        os.makedirs(os.path.join(self.marketplace_dir, "proxy"), exist_ok=True)
        self.uninstall_copy = os.path.join(self.marketplace_dir, "uninstall.sh")
        shutil.copy(UNINSTALL, self.uninstall_copy)
        shutil.copy(os.path.join(REPO, "hooks", "chain.sh"),
                    os.path.join(self.marketplace_dir, "hooks", "chain.sh"))
        shutil.copy(CHAIN, os.path.join(self.marketplace_dir, "proxy", "chain.py"))
        os.chmod(self.uninstall_copy, 0o755)
        os.chmod(os.path.join(self.marketplace_dir, "hooks", "chain.sh"), 0o755)

    def _env(self):
        env = dict(os.environ, HOME=self.home, ROLLING_CONTEXT_NO_START="1")
        for key in AMBIENT:
            env.pop(key, None)
        return env

    def _local_env(self):
        with open(self.local, encoding="utf-8") as f:
            return json.load(f).get("env", {})

    def _chain(self):
        result = subprocess.run([sys.executable, CHAIN, "chain", "--yes"], cwd=self.project,
                                 env=self._env(), capture_output=True, text=True, timeout=30)
        return result

    def _uninstall(self, env=None):
        return subprocess.run([BASH, self.uninstall_copy],
                               env=env if env is not None else self._env(),
                               capture_output=True, text=True, timeout=60)

    def test_uninstall_after_chain_does_not_strand_the_project(self):
        chained = self._chain()
        self.assertEqual(chained.returncode, 0, chained.stderr)
        self.assertIn("5588", self._local_env()["ANTHROPIC_BASE_URL"])

        result = self._uninstall()
        self.assertEqual(result.returncode, 0, result.stderr)
        # The project must not be left pointing at a proxy that no longer exists.
        self.assertNotIn("5588", self._local_env().get("ANTHROPIC_BASE_URL", ""))

    def test_state_file_is_removed(self):
        chained = self._chain()
        self.assertEqual(chained.returncode, 0, chained.stderr)

        result = self._uninstall()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = os.path.join(self.home, ".claude", "rolling-context-proxy.json")
        self.assertFalse(os.path.exists(state))

    def test_a_skipped_step_is_reported_not_silent(self):
        # A PATH with coreutils but no python3/python: the settings.json cleanup guard
        # must say so, not skip silently under set -e.
        with open(os.path.join(self.home, ".claude", "settings.json"), "w", encoding="utf-8") as f:
            json.dump({}, f)
        binless = tempfile.mkdtemp(prefix="uninstall-path-")
        self.addCleanup(shutil.rmtree, binless, ignore_errors=True)
        for tool in ("rm", "grep", "lsof", "ss"):
            found = shutil.which(tool)
            if found:
                os.symlink(found, os.path.join(binless, tool))
        env = dict(self._env(), PATH=binless)
        result = self._uninstall(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SKIPPED", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
