"""Task 5: prompts mandate oldest-first decay + invariant preservation and
drop the append-only language.

Run: python3 -m unittest tests.test_decay_contract -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import compressor  # noqa: E402


class DecayContractTest(unittest.TestCase):
    def test_native_prompt_mandates_oldest_first_decay(self):
        p = compressor.NATIVE_COMPACT_PROMPT
        self.assertIn("OLDEST", p)
        self.assertIn("Timeline", p)

    def test_native_prompt_preserves_invariants(self):
        p = compressor.NATIVE_COMPACT_PROMPT
        self.assertIn("Active Goal", p)
        self.assertIn("Key Details", p)

    def test_append_only_language_removed(self):
        p = compressor.NATIVE_COMPACT_PROMPT
        self.assertNotIn("copy it forward exactly", p)

    def test_summary_rules_declare_budget(self):
        self.assertIn("16,000", compressor.SUMMARY_RULES)


if __name__ == "__main__":
    unittest.main()
