"""Concurrency test: try_begin_compression() must admit exactly one starter.

The proxy spawns a background compression (a real, billed upstream API call)
when a request crosses the token trigger. ThreadedHTTPServer serves one thread
per request, so two over-trigger requests can reach the trigger block at the
same instant. The old check ("any live thread") plus a thread assigned AFTER
start() left a window where a just-added entry (thread=None) was invisible to a
concurrent scan, so both requests spawned a compression = duplicate cost.

Invariant proven here: with N threads released simultaneously by a Barrier,
CompressionStore.try_begin_compression() returns a live entry to EXACTLY ONE
caller and None to the rest, and the store holds exactly one in-progress entry.

Run: python3 -m unittest discover -s tests
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
from server import CompressionStore  # noqa: E402


class TryBeginCompressionRaceTest(unittest.TestCase):
    def test_barrier_synchronized_single_starter(self):
        n = 32
        store = CompressionStore()
        barrier = threading.Barrier(n)
        results = [None] * n

        def worker(i):
            # Release all threads into try_begin_compression() at the same instant
            # so the check-add-mark critical section is genuinely contended.
            barrier.wait()
            results[i] = store.try_begin_compression()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [r for r in results if r is not None]
        self.assertEqual(
            len(winners), 1,
            f"expected exactly one starter, got {len(winners)}",
        )
        # The reservation is visible BEFORE any thread is assigned: the winning
        # entry is marked in_progress and its thread slot is still None (the
        # caller assigns it only after start()).
        won = winners[0]
        self.assertTrue(won["in_progress"])
        self.assertIsNone(won["thread"])

        in_progress = [e for e in store.compressions if e.get("in_progress")]
        self.assertEqual(
            len(in_progress), 1,
            f"expected one in-progress entry in store, got {len(in_progress)}",
        )

    def test_second_call_blocked_until_flag_cleared(self):
        store = CompressionStore()
        first = store.try_begin_compression()
        self.assertIsNotNone(first)
        # A second attempt while the first is still in progress is refused.
        self.assertIsNone(store.try_begin_compression())
        # Once the in-progress flag clears (compression finished/failed), a new
        # compression may begin again.
        first["in_progress"] = False
        second = store.try_begin_compression()
        self.assertIsNotNone(second)


if __name__ == "__main__":
    unittest.main()
