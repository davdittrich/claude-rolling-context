"""Tests for Gemini-e86.11: reduce native summary drift via the PROMPT only.

Invariant proven here: NATIVE_COMPACT_PROMPT mandates that a leading prior
[ROLLING_CONTEXT_SUMMARY] block be carried forward and extended with
oldest-first Timeline decay, WITHOUT changing the span native sends
byte-identical, preserving the prompt-cache read). This is a structural test:
it proves the instruction text is present and that the assembled output still
contains exactly one summary block. It does NOT assert "drift is reduced" —
that is a model-behavior claim unprovable without a real backend.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import compressor  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeSummarizerConn  # noqa: E402


class NativeCompactPromptTextTest(unittest.TestCase):
    """The prompt string itself must mandate verbatim carry-forward."""

    def test_mandates_oldest_first_decay(self):
        prompt = compressor.NATIVE_COMPACT_PROMPT
        self.assertIn(compressor.SUMMARY_MARKER, prompt)
        self.assertIn("OLDEST", prompt)
        self.assertIn("MERGE", prompt)

    def test_preserves_invariants_and_recent(self):
        prompt = compressor.NATIVE_COMPACT_PROMPT
        self.assertIn("Active Goal", prompt)
        self.assertIn("Key Details", prompt)
        self.assertIn("recent", prompt.lower())


PRIOR_SUMMARY_TEXT = "PRIOR_SUMMARY_UNIQUE_MARKER_TOKEN: user asked for X, file foo.py changed."


def _build_messages():
    """9 messages: [prior summary, ack, 5 turns of new conversation, ...]
    shaped so compress() with keep_floor=2/keep_turns=2 cuts at index 4 --
    i.e. native summarizes messages[:4] (prior summary + ack + one new
    exchange) and keeps messages[4:] verbatim. See compress()'s
    _find_keep_index/_safe_cut for why this exact shape lands on cut=4."""
    return [
        {
            "role": "user",
            "content": (
                f"{compressor.SUMMARY_MARKER}\n{PRIOR_SUMMARY_TEXT}\n"
                f"{compressor.SUMMARY_END_MARKER}\n\n"
                "The above is a chronological summary of our earlier conversation. "
                "Continue from where we left off."
            ),
        },
        {"role": "assistant", "content": "I have the full context. Continuing."},
        {"role": "user", "content": "message 2 " * 50},
        {"role": "assistant", "content": "message 3 " * 50},
        {"role": "user", "content": "message 4 " * 50},
        {"role": "assistant", "content": "message 5 " * 50},
        {"role": "user", "content": "message 6 " * 50},
        {"role": "assistant", "content": "message 7 " * 50},
        {"role": "user", "content": "message 8 (most recent)"},
    ]


class NativeCarryForwardStructuralTest(unittest.TestCase):
    def setUp(self):
        self._real_conn_fn = compressor._summarizer_conn
        self._fake_conn = FakeSummarizerConn(
            reply_text="NEW mocked summary of the appended events.", capture=True
        )
        compressor._summarizer_conn = lambda timeout=600: self._fake_conn

    def tearDown(self):
        compressor._summarizer_conn = self._real_conn_fn

    def test_prompt_sent_carries_prior_summary_and_instruction_verbatim(self):
        comp = compressor.RollingCompressor(keep_floor=2, keep_turns=2)
        messages = _build_messages()
        payload = {"model": "claude-sonnet-4-5-20250929"}

        result = comp.compress(messages, auth_headers={}, real_token_count=None, payload=payload)
        self.assertIsNotNone(result)

        sent_messages = self._fake_conn.last_body["messages"]

        # Native must still send messages[:cut] byte-identical (span/prefix
        # unchanged -> cache read preserved). cut=4 here, so the first two
        # sent messages must be exactly the original prior summary + ack.
        self.assertEqual(sent_messages[0]["content"], messages[0]["content"])
        self.assertEqual(sent_messages[1]["content"], messages[1]["content"])
        self.assertIn(PRIOR_SUMMARY_TEXT, sent_messages[0]["content"])

        # The compact instruction (containing the verbatim-carry-forward
        # mandate) must be present, byte-identical, as its own message.
        self.assertEqual(sent_messages[-1]["content"], compressor.NATIVE_COMPACT_PROMPT)
        self.assertIn("OLDEST", sent_messages[-1]["content"])

    def test_assembled_output_has_exactly_one_summary_block(self):
        comp = compressor.RollingCompressor(keep_floor=2, keep_turns=2)
        messages = _build_messages()
        payload = {"model": "claude-sonnet-4-5-20250929"}

        result = comp.compress(messages, auth_headers={}, real_token_count=None, payload=payload)
        self.assertIsNotNone(result)

        all_text = json.dumps(result)
        self.assertEqual(all_text.count(compressor.SUMMARY_MARKER), 1)
        self.assertEqual(all_text.count(compressor.SUMMARY_END_MARKER), 1)

        # And the new summary result[0] must be a user message wrapping the
        # (mocked) new summary in the marker pair -- not the recent messages.
        self.assertEqual(result[0]["role"], "user")
        self.assertIn(compressor.SUMMARY_MARKER, result[0]["content"])


if __name__ == "__main__":
    unittest.main()
