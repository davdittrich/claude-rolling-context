"""Task 6: flattened summarizer requests max_tokens=20000.

Run: python3 -m unittest tests.test_flattened_cap -v
"""
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import compressor  # noqa: E402


class FlattenedCapTest(unittest.TestCase):
    def test_flattened_cap_is_20000(self):
        src = inspect.getsource(compressor.RollingCompressor._summarize_flattened)
        self.assertIn("summary_max_tokens = 20000", src)
        self.assertNotIn("summary_max_tokens = 16000", src)


if __name__ == "__main__":
    unittest.main()
