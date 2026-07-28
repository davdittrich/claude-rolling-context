# tests/test_entry_point.py -- the file the error message names must exist and work.
import os, re, subprocess, unittest
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
