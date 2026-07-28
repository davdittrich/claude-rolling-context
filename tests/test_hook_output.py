"""SessionStart emits exactly one JSON object, or nothing (Fact 2), and alerts on displacement.

Drives hooks/start-proxy.sh for real, with ROLLING_CONTEXT_NO_START set so the detection and
seeding logic runs and the daemon spawn does not.

Hermetic: HOME is a fresh tempdir and the four ambient upstream-resolution variables this
machine exports are stripped from the child environment -- mirroring _fakes.hermetic_home(),
which cannot be reused directly because these tests cross a subprocess boundary.

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


class HookOutputTest(unittest.TestCase):
    def setUp(self):
        self.home = self._tempdir("hook-home-")
        self.project = self._tempdir("hook-proj-")
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

    def _run_in(self, project):
        return subprocess.run(["bash", HOOK], cwd=project, env=self._env(),
                              capture_output=True, text=True, timeout=30)

    def _run(self):
        return self._run_in(self.project)

    def _write_abu(self, project, url):
        with open(os.path.join(project, ".claude", "settings.local.json"), "w") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": url}}, f)

    def _displace(self):
        self._write_abu(self.project, FOREIGN)

    def _new_project_displaced_by(self, url):
        project = self._tempdir("hook-proj-")
        os.makedirs(os.path.join(project, ".claude"), exist_ok=True)
        self._write_abu(project, url)
        return project

    def _chain(self):
        return subprocess.run([sys.executable, CHAIN, "chain", "--yes"], cwd=self.project,
                              env=self._env(), capture_output=True, text=True, timeout=30)

    def test_displacement_emits_one_json_object_with_both_fields(self):
        self._displace()
        out = self._run().stdout.strip()
        payload = json.loads(out)          # exactly one object, or this raises
        self.assertIn("systemMessage", payload)
        self.assertIn("hookSpecificOutput", payload)
        self.assertIn("8787", payload["systemMessage"])

    def test_a_project_local_displacement_is_seen_at_all(self):
        # Root bug #2: the hook read only ~/.claude/settings.json and printed "already".
        self._displace()
        self.assertIn("8787", self._run().stdout)

    def test_loopback_foreign_proxy_is_not_mistaken_for_us(self):
        # Root bug #1: `elif "127.0.0.1" not in existing` treated headroom as ourselves.
        # The "already" half is now unfalsifiable on its own -- no diagnostic reaches stdout
        # any more -- so it is paired with the positive: classified foreign, and said so.
        self._displace()
        out = self._run().stdout
        self.assertNotIn("already", out.lower())
        self.assertIn("8787", out)

    def test_a_loopback_alias_for_our_own_port_is_us(self):
        # The other direction of the same predicate: is_self is not a substring test either
        # way. localhost:5588 is us, and must stay silent.
        self._write_abu(self.project, "http://localhost:5588")
        self.assertEqual(self._run().stdout.strip(), "")

    def test_a_foreign_loopback_value_is_never_overwritten(self):
        # The other half of root bug #1: mistaking headroom for us also meant we happily
        # left/overwrote the variable without ever recording what we displaced.
        self._displace()
        self._run()
        path = os.path.join(self.project, ".claude", "settings.local.json")
        with open(path) as f:
            self.assertEqual(json.load(f)["env"]["ANTHROPIC_BASE_URL"], FOREIGN)

    def test_quiet_when_we_are_in_the_path(self):
        self._write_abu(self.project, "http://127.0.0.1:5588")
        self.assertEqual(self._run().stdout.strip(), "")

    def test_unset_seeds_our_own_url_in_user_scope(self):
        self.assertEqual(self._run().stdout.strip(), "")
        with open(os.path.join(self.home, ".claude", "settings.json")) as f:
            env = json.load(f)["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:5588")
        self.assertNotIn("ROLLING_CONTEXT_UPSTREAM", env)

    def test_diagnostics_never_reach_stdout(self):
        self._displace()
        result = self._run()
        for line in result.stdout.splitlines():
            self.assertTrue(line.strip().startswith("{") or not line.strip())

    def test_the_second_session_is_silent(self):
        self._displace()
        self.assertIn("8787", self._run().stdout)
        self.assertEqual(self._run().stdout.strip(), "")

    def test_a_different_project_is_alerted_for_the_same_url(self):
        # Precisely why the key is {project, url} and not url alone.
        self._displace()
        self._run()
        other = self._new_project_displaced_by(FOREIGN)
        self.assertIn("8787", self._run_in(other).stdout)

    def test_a_displaced_chain_re_alerts_with_different_wording(self):
        self._displace()
        self.assertEqual(self._chain().returncode, 0)
        self._displace()
        self.assertIn("overwritten", self._run().stdout)


if __name__ == "__main__":
    unittest.main()
