# tests/test_entry_point.py -- the file the error message names must exist and work.
import os, re, subprocess, tempfile, unittest
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class EntryPointTest(unittest.TestCase):
    def test_chain_sh_exists_and_is_executable(self):
        path = os.path.join(REPO, "hooks", "chain.sh")
        self.assertTrue(os.path.exists(path))
        self.assertTrue(os.access(path, os.X_OK))

    def test_it_routes_to_the_python_implementation(self):
        # Hermetic: pin the default port explicitly rather than inherit whatever
        # ROLLING_CONTEXT_PORT the calling shell happens to export.
        env = dict(os.environ)
        env.pop("ROLLING_CONTEXT_PORT", None)
        out = subprocess.run(["bash", os.path.join(REPO, "hooks", "chain.sh"),
                              "is-self", "http://127.0.0.1:5588"],
                             capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(out.returncode, 0)

    def test_every_path_named_in_an_error_message_exists(self):
        # The dead-upstream body names hooks/chain.sh. If that ever moves, this fails.
        with open(os.path.join(REPO, "proxy", "server.py"), encoding="utf-8") as f:
            src = f.read()
        for rel in set(re.findall(r"hooks/[a-z-]+\.sh", src)):
            self.assertTrue(os.path.exists(os.path.join(REPO, rel)), f"{rel} is named but missing")

    def test_every_verb_runs_without_a_python_traceback(self):
        # C1 regression guard: the __main__ dispatch used to sit above every Task-6
        # definition, so every verb but is-self died with NameError as a script --
        # invisible to test_it_routes_to_the_python_implementation, which only exercises
        # is-self, and to the do_chain/do_unchain/do_status tests, which call the Python
        # implementation as an import (the module is already fully loaded by then).
        home = tempfile.mkdtemp(prefix="chain-entry-home-")
        project = tempfile.mkdtemp(prefix="chain-entry-proj-")
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        os.makedirs(os.path.join(project, ".claude"), exist_ok=True)
        env = dict(os.environ)
        env["HOME"] = home
        for var in ("ANTHROPIC_BASE_URL", "ROLLING_CONTEXT_UPSTREAM", "ROLLING_CONTEXT_PORT"):
            env.pop(var, None)
        script = os.path.join(REPO, "hooks", "chain.sh")
        calls = [
            ("is-self", ["http://127.0.0.1:5588"]),
            ("chain", ["--yes"]),
            ("status", []),
            ("unchain", []),
            ("effective-abu", []),
        ]
        for verb, args in calls:
            out = subprocess.run(["bash", script, verb, *args], cwd=project,
                                  capture_output=True, text=True, timeout=30, env=env)
            self.assertNotIn("Traceback", out.stderr, f"{verb}: {out.stderr}")
            self.assertNotIn("NameError", out.stderr, f"{verb}: {out.stderr}")
            self.assertEqual(out.returncode, 0,
                              f"{verb}: rc={out.returncode} stderr={out.stderr}")
