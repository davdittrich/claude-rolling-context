"""Tests for the single guarded deepcopy of convo[-1] in _summarize_native.

_summarize_native previously deep-copied convo[-1] twice when the trailing
message hit both the cache-control breakpoint block (block-1, ~L385) and the
compact-merge block (block-2, ~L402): once in each block, always. Block-1 is
guarded and may be skipped once 4 cache_control breakpoints already exist in
the payload/convo -- when it is skipped, convo[-1] is still the caller's own
dict aliased in via `convo = list(messages[:cut])` (a shallow copy of the
list, not of its elements). Block-2 must therefore keep deep-copying in that
case; reusing block-1's copy is only safe when block-1 actually ran and
privatized convo[-1].

These tests prove, for both cases (block-1 ran / block-1 skipped):
- the outbound trailing message matches the exact expected merged+breakpoint
  shape (the guarded single-copy produces identical output to the prior
  always-double-copy code), and
- the caller's original `messages[cut-1]` object is never mutated.

Run: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import compressor  # noqa: E402
from compressor import NATIVE_COMPACT_PROMPT  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeSummarizerConn  # noqa: E402


class NativeDedupCopyTest(unittest.TestCase):
    def setUp(self):
        self._fake_conn = FakeSummarizerConn(capture=True)
        self._real_conn_fn = compressor._summarizer_conn
        compressor._summarizer_conn = lambda ep, timeout=600: self._fake_conn

    def tearDown(self):
        compressor._summarizer_conn = self._real_conn_fn

    def test_block1_ran_merges_onto_the_privatized_breakpoint_copy(self):
        # No pre-existing breakpoints -> block-1 runs and privatizes
        # convo[-1] with a cache_control block; block-2 must reuse that
        # same private copy rather than deep-copying it again.
        comp = compressor.RollingCompressor()
        payload = {"model": "claude-sonnet-4-5-20250929"}
        original_last = {"role": "user", "content": "turn 2"}
        messages = [
            {"role": "user", "content": "turn 1"},
            {"role": "assistant", "content": "reply 1"},
            original_last,
        ]

        comp._summarize_native(payload, messages, cut=3, auth_headers={})

        sent = self._fake_conn.last_body["messages"]
        self.assertEqual(
            sent[-1],
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "turn 2",
                        "cache_control": {"type": "ephemeral"},
                    },
                    {"type": "text", "text": NATIVE_COMPACT_PROMPT},
                ],
            },
        )
        # Caller's own message object/list untouched.
        self.assertEqual(messages[2], {"role": "user", "content": "turn 2"})
        self.assertIs(messages[2], original_last)

    def test_block1_skipped_still_deep_copies_before_merging(self):
        # 4 pre-existing cache_control breakpoints on payload["system"] ->
        # _count_breakpoints returns 4, block-1's `if ... < 4` is False, so
        # it is skipped and convo[-1] stays aliased to the caller's dict
        # until block-2. Block-2 must still deep-copy in this branch.
        comp = compressor.RollingCompressor()
        payload = {
            "model": "claude-sonnet-4-5-20250929",
            "system": [
                {"type": "text", "text": f"s{i}", "cache_control": {"type": "ephemeral"}}
                for i in range(4)
            ],
        }
        original_last = {"role": "user", "content": "turn 2"}
        messages = [
            {"role": "user", "content": "turn 1"},
            {"role": "assistant", "content": "reply 1"},
            original_last,
        ]

        comp._summarize_native(payload, messages, cut=3, auth_headers={})

        sent = self._fake_conn.last_body["messages"]
        self.assertEqual(
            sent[-1],
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "turn 2"},
                    {"type": "text", "text": NATIVE_COMPACT_PROMPT},
                ],
            },
        )
        # No breakpoint was applied (block-1 skipped) -- and the caller's
        # own message object/list must still be untouched, proving block-2
        # deep-copied rather than mutating the aliased dict in place.
        self.assertEqual(messages[2], {"role": "user", "content": "turn 2"})
        self.assertIs(messages[2], original_last)


if __name__ == "__main__":
    unittest.main()
