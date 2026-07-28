"""State file I/O: atomic, locked, 0600, refusing rather than overwriting (spec section 5).

Run: python3 -m unittest discover -s tests
"""
import os
import stat
import tempfile
import unittest
from unittest import mock

from proxy import chain


class StateIOTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="state-")
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        patch = mock.patch.dict(os.environ, {"HOME": self.home}, clear=False)
        patch.start()
        # Hermetic: this machine may export ANTHROPIC_BASE_URL (headroom does).
        # patch.dict restores the whole mapping on stop, so these pops are undone with it.
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        os.environ.pop("ROLLING_CONTEXT_UPSTREAM", None)
        self.addCleanup(patch.stop)

    def test_absent_state_reads_as_empty(self):
        self.assertEqual(chain.load_state(), chain.empty_state())

    def test_round_trip_both_fields(self):
        state = chain.empty_state()
        state["abu"] = {"/p": {"path": "/p/.claude/settings.local.json",
                               "wrote": "http://127.0.0.1:5588",
                               "displaced": "http://127.0.0.1:8787"}}
        state["upstream"] = {"wrote": "http://127.0.0.1:8787"}
        chain.save_state(state)
        self.assertEqual(chain.load_state(), state)

    def test_upstream_has_no_displaced_field(self):
        # It is our own key. Recording a value to restore to is what produced the
        # ordering bug an earlier draft had -- the field is deliberately absent.
        state = chain.empty_state()
        state["upstream"] = {"wrote": "http://127.0.0.1:8787"}
        chain.save_state(state)
        self.assertNotIn("displaced", chain.load_state()["upstream"])

    def test_written_mode_is_0600(self):
        chain.save_state(chain.empty_state())
        self.assertEqual(stat.S_IMODE(os.stat(chain.state_path()).st_mode), 0o600)

    def test_rewrite_keeps_mode_0600(self):
        chain.save_state(chain.empty_state())
        chain.save_state(chain.empty_state())
        self.assertEqual(stat.S_IMODE(os.stat(chain.state_path()).st_mode), 0o600)

    def test_rewrite_tightens_a_loose_pre_existing_file(self):
        # A plain in-place open(path, "w") would inherit an existing file's mode
        # instead of replacing the inode -- this is the test that would catch that.
        with open(chain.state_path(), "w", encoding="utf-8") as f:
            f.write("{}")
        os.chmod(chain.state_path(), 0o644)
        chain.save_state(chain.empty_state())
        self.assertEqual(stat.S_IMODE(os.stat(chain.state_path()).st_mode), 0o600)

    def test_no_temp_file_is_left_behind(self):
        chain.save_state(chain.empty_state())
        leftovers = [n for n in os.listdir(os.path.join(self.home, ".claude"))
                     if n.startswith(".rolling-context-state-")]
        self.assertEqual(leftovers, [])

    def test_unparseable_state_refuses_rather_than_overwriting(self):
        with open(chain.state_path(), "w", encoding="utf-8") as f:
            f.write("{ broken")
        with self.assertRaises(chain.UnparseableSettings):
            chain.load_state()
        with open(chain.state_path(), encoding="utf-8") as f:
            self.assertEqual(f.read(), "{ broken")

if __name__ == "__main__":
    unittest.main()
