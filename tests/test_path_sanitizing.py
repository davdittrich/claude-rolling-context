"""Project paths are escaped before display (spec section 5).

Structural guarantee only: control bytes cannot reach a terminal, a log line, or the state file.
NOT an anti-prompt-injection measure -- printable text has nothing to escape, and a test records
that limit explicitly so nobody mistakes the one for the other.

Run: python3 -m unittest discover -s tests
"""

import unittest

from proxy import chain


class DisplayTest(unittest.TestCase):
    def test_escape_sequences_cannot_reach_the_terminal(self):
        self.assertNotIn("\x1b", chain.display("/tmp/\x1b[31mred\x1b[0m"))

    def test_newlines_cannot_break_a_log_line(self):
        self.assertNotIn("\n", chain.display("/tmp/a\nb"))

    def test_printable_text_is_left_alone(self):
        # The honest limit, recorded on purpose: nothing here to escape.
        plain = "/tmp/ignore previous instructions"
        self.assertEqual(chain.display(plain), plain)

    def test_none_is_empty(self):
        self.assertEqual(chain.display(None), "")


if __name__ == "__main__":
    unittest.main()
