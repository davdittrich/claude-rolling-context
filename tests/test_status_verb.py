"""status: reports, never writes (spec section 6).

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


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="status-home-")
        self.project = tempfile.mkdtemp(prefix="status-proj-")
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

    def _run(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = chain.do_status(self.project)
        return code, buf.getvalue()

    def test_reports_the_displacing_proxy_and_its_source_file(self):
        local = os.path.join(self.project, ".claude", "settings.local.json")
        with open(local, "w", encoding="utf-8") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": FOREIGN}}, f)
        _, out = self._run()
        self.assertIn("8787", out)
        self.assertIn(local, out)

    def test_writes_nothing(self):
        local = os.path.join(self.project, ".claude", "settings.local.json")
        with open(local, "w", encoding="utf-8") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": FOREIGN}}, f)
        chain.do_chain(self.project, assume_yes=True)
        before_state = open(chain.state_path(), "rb").read()
        before_local = open(local, "rb").read()
        self._run()
        self.assertEqual(open(chain.state_path(), "rb").read(), before_state)
        self.assertEqual(open(local, "rb").read(), before_local)

    def test_reports_the_recorded_upstream(self):
        state = chain.empty_state()
        state["upstream"] = {"wrote": FOREIGN}
        chain.save_state(state)
        _, out = self._run()
        self.assertIn("8787", out)


if __name__ == "__main__":
    unittest.main()
