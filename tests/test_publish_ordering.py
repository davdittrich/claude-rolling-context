"""Regression: _do_background_compression must publish `pending_hashes` BEFORE
the guard field `pending` (Gemini-e86.15 / whole-branch review M1).

The promote loop in `_handle_messages` runs on a different, unlocked request
thread; it guards on `pending is not None` then reads `pending_hashes`. If the
background thread wrote `pending` first, an interleave could promote a compression
with `pending_hashes` still None -> original_hashes=None -> find_match never
matches -> compression wasted. This test records the field write order and asserts
the guard field is written last on the success path.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import server  # noqa: E402


class RecordingDict(dict):
    """A dict that records the order of __setitem__ keys."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.write_order = []

    def __setitem__(self, key, value):
        self.write_order.append(key)
        super().__setitem__(key, value)


class PublishOrderingTest(unittest.TestCase):
    def test_pending_hashes_published_before_pending_guard(self):
        entry = RecordingDict(
            prefix=None, original_hashes=None,
            pending=None, pending_hashes=None,
            thread=None, in_progress=True,
        )
        messages = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        # compress -> [summary, ack, recent]; prefix = first two.
        fake_compressed = [
            {"role": "user", "content": "summary"},
            {"role": "assistant", "content": "ack"},
            {"role": "user", "content": "recent"},
        ]
        with patch.object(server.compressor, "compress", return_value=fake_compressed), \
             patch.object(server, "_hash_messages", return_value=["h1"]):
            server._do_background_compression(entry, messages, {"authorization": "x"})

        wo = entry.write_order
        self.assertIn("pending", wo)
        self.assertIn("pending_hashes", wo)
        self.assertLess(
            wo.index("pending_hashes"), wo.index("pending"),
            f"pending_hashes must be published before the guard field 'pending'; got {wo}",
        )
        # And the success path must leave both set (no None guard leak).
        self.assertIsNotNone(entry["pending"])
        self.assertIsNotNone(entry["pending_hashes"])


if __name__ == "__main__":
    unittest.main()
