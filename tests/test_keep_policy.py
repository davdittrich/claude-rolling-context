"""Tests for the blend keep policy in RollingCompressor._find_keep_index.

Invariants proven here:
- kept user-turns is in [keep_floor, keep_turns] (floor overrides the char hi;
  keep_turns caps upward), and the returned cut is a _safe_cut no-op so the cap
  survives compress()'s post-processing.
- the char budget (target) acts as the soft upper ceiling in the normal regime.
- misconfig (keep_floor > keep_turns) is clamped.
- existing summary prefix and degenerate inputs are handled safely.

Run: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
from compressor import RollingCompressor  # noqa: E402


def u(text):
    """A plain user message (a clean turn boundary)."""
    return {"role": "user", "content": text}


def a(text):
    """A plain assistant message (no tool_use)."""
    return {"role": "assistant", "content": text}


def tool_result_user(text):
    """A user message carrying a tool_result (NOT a clean boundary)."""
    return {"role": "user", "content": [{"type": "tool_result", "content": text}]}


def tool_use_assistant(text):
    """An assistant message carrying a tool_use (its successor is unclean)."""
    return {"role": "assistant", "content": [{"type": "tool_use", "input": {"t": text}}]}


def turns(n, size=200):
    """n plain [user, assistant] turns; boundaries at every even index."""
    msgs = []
    for i in range(n):
        msgs.append(u(f"user {i} " + "x" * size))
        msgs.append(a(f"asst {i} " + "y" * size))
    return msgs


def count_user_boundaries(comp, messages, cut):
    """Number of clean-user-boundary turns retained in messages[cut:]."""
    kept = messages[cut:]
    return sum(
        1 for i, m in enumerate(kept)
        if m.get("role") == "user" and not comp._has_tool_result(m)
    )


class BlendKeepPolicyTest(unittest.TestCase):
    def make(self, **kw):
        kw.setdefault("target_tokens", 40000)
        kw.setdefault("keep_turns", 8)
        kw.setdefault("keep_floor", 3)
        return RollingCompressor(**kw)

    def test_n_cap_and_safecut_noop(self):
        """char budget non-binding -> kept limited by keep_turns; cut is a no-op."""
        comp = self.make()
        msgs = turns(20, size=200)
        cut = comp._find_keep_index(msgs, keep_ratio=1.0)  # target == total, never binds
        self.assertEqual(count_user_boundaries(comp, msgs, cut), 8)
        # _safe_cut must not move an already-clean cut (=> kept<=N survives compress)
        self.assertEqual(comp._safe_cut(msgs, cut, 0), cut)

    def test_floor_overrides_giant_dump(self):
        """one giant turn would blow the char budget after 1 turn; floor forces >=3."""
        comp = self.make(keep_floor=3, keep_turns=8)
        msgs = turns(5, size=100)
        # inflate the 3rd-from-newest assistant turn into a giant dump
        msgs[5] = a("asst 2 " + "z" * 500000)
        cut = comp._find_keep_index(msgs, keep_ratio=0.001)  # tiny target
        kept = count_user_boundaries(comp, msgs, cut)
        self.assertGreaterEqual(kept, 3)
        # the giant dump's own turn (user 2 / asst 2) is retained, not dropped alone
        self.assertIn(msgs[4], msgs[cut:])

    def test_token_hi_binds_in_normal_regime(self):
        """medium turns: char budget caps kept strictly between floor and keep_turns."""
        comp = self.make(keep_floor=3, keep_turns=8)
        msgs = turns(12, size=1000)
        total = comp._count_chars(msgs)
        # target ~ 4 recent turns' worth of chars
        four_turns = comp._count_chars(msgs[-8:])
        cut = comp._find_keep_index(msgs, keep_ratio=four_turns / total)
        kept = count_user_boundaries(comp, msgs, cut)
        self.assertGreaterEqual(kept, 3)
        self.assertLess(kept, 8)

    def test_boundary_cleanliness(self):
        comp = self.make()
        msgs = turns(15)
        cut = comp._find_keep_index(msgs, keep_ratio=0.3)
        self.assertEqual(msgs[cut].get("role"), "user")
        self.assertFalse(comp._has_tool_result(msgs[cut]))
        self.assertFalse(comp._has_tool_use(msgs[cut - 1]))

    def test_misconfig_floor_clamped(self):
        comp = self.make(keep_turns=8, keep_floor=10)
        self.assertEqual(comp.keep_floor, 8)
        self.assertEqual(comp.keep_turns, 8)
        comp2 = self.make(keep_turns=5, keep_floor=0)
        self.assertGreaterEqual(comp2.keep_floor, 1)

    def test_existing_summary_prefix_not_orphaned(self):
        comp = self.make(keep_floor=3, keep_turns=8)
        msgs = [u("[SUMMARY]...[/SUMMARY]"), a("ack")] + turns(15)
        cut = comp._find_keep_index(msgs, keep_ratio=0.2)
        # compress passes start_idx=2 to _safe_cut; the prefix must never be orphaned
        safe = comp._safe_cut(msgs, cut, 2)
        self.assertGreaterEqual(safe, 2)

    def test_empty_boundaries_returns_zero(self):
        """no clean boundary (every user msg is a tool_result) -> 0, no IndexError."""
        comp = self.make()
        msgs = []
        for i in range(8):
            msgs.append(tool_use_assistant(f"call {i}"))
            msgs.append(tool_result_user("r" * 100))
        self.assertEqual(comp._find_keep_index(msgs, keep_ratio=0.3), 0)

    def test_small_conversation_returns_zero(self):
        comp = self.make()
        self.assertEqual(comp._find_keep_index(turns(2), keep_ratio=0.5), 0)


if __name__ == "__main__":
    unittest.main()
