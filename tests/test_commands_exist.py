"""Every command the alert or status text names must exist as a slash command (D6, spec section 4).

Run: python3 -m unittest discover -s tests
"""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sources():
    for rel in ("proxy/chain.py", "hooks/start-proxy.sh", "install.sh"):
        with open(os.path.join(REPO, rel), encoding="utf-8") as f:
            yield rel, f.read()


class CommandsExistTest(unittest.TestCase):
    def test_every_named_slash_command_has_a_file(self):
        named = set()
        for rel, text in _sources():
            named |= set(re.findall(r"/rolling-context:([a-z-]+)", text))
        self.assertTrue(named, "expected the alert or status text to name a slash command")
        for verb in sorted(named):
            path = os.path.join(REPO, "commands", f"{verb}.md")
            self.assertTrue(os.path.exists(path), f"{verb} is named but commands/{verb}.md is missing")

    def test_each_command_invokes_the_shell_implementation(self):
        for verb in ("chain", "unchain", "status"):
            with open(os.path.join(REPO, "commands", f"{verb}.md"), encoding="utf-8") as f:
                body = f.read()
            self.assertIn("chain.py", body, f"commands/{verb}.md must call the one implementation")

    def test_version_was_bumped(self):
        import json
        with open(os.path.join(REPO, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["version"], "2.3.0")


if __name__ == "__main__":
    unittest.main()
