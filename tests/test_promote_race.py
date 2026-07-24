"""Concurrency test: promoting a pending compression must be atomic.

The old promote logic lived inline in _handle_messages as an UNLOCKED loop
over store.compressions:

    for entry in store.compressions:
        if entry["pending"] is not None:      # guard
            entry["prefix"] = entry["pending"]            # re-read
            entry["original_hashes"] = entry["pending_hashes"]
            entry["pending"] = None
            entry["pending_hashes"] = None

store.compressions returns a list SNAPSHOT, but the entry DICTS inside are
shared. ThreadedHTTPServer serves one thread per request and Claude Code fires
parallel /v1/messages traffic, so two request threads run this loop at once.
The guard + four assignments are separate bytecodes, so the GIL does NOT make
them atomic: thread B can pass the `pending is not None` guard, then read
entry["pending"] AFTER thread A has already set it to None -> prefix=None and
original_hashes=None. find_match does `if not oh: continue`, so the entry is
permanently unusable: the billed background compression is wasted AND the
over-trigger request is forwarded uncompressed.

Fix: CompressionStore.promote_pending() performs the whole field transition
under self._lock, so promotion is atomic and exactly one caller promotes each
pending entry.

Invariant proven here: with N threads released simultaneously by a Barrier,
across every round EXACTLY ONE promote_pending() call transitions the pending
entry, and the entry always ends with prefix == the pending value and
original_hashes == pending_hashes (never None).

Run: python3 -m unittest discover -s tests
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
from server import CompressionStore  # noqa: E402


class PromotePendingRaceTest(unittest.TestCase):
    def test_barrier_synchronized_atomic_promotion(self):
        n = 32
        rounds = 50
        store = CompressionStore()
        entry = store.add()

        sentinel_prefix = [
            {"role": "user", "content": "SUMMARY"},
            {"role": "assistant", "content": "ACK"},
        ]
        sentinel_hashes = ["h0", "h1", "h2"]

        for r in range(rounds):
            # Arm one pending compression for this round.
            entry["prefix"] = None
            entry["original_hashes"] = []
            entry["pending_hashes"] = list(sentinel_hashes)
            entry["pending"] = list(sentinel_prefix)

            barrier = threading.Barrier(n)
            counts = [0] * n

            def worker(i):
                # Release all threads into promote_pending() at the same instant
                # so the guard/transition critical section is genuinely contended.
                barrier.wait()
                counts[i] = store.promote_pending()

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # Exactly one caller promoted the pending entry this round. An
            # unlocked loop lets two threads both pass the guard and both
            # promote (total > 1) or corrupt the entry.
            self.assertEqual(
                sum(counts), 1,
                f"round {r}: expected exactly one promotion, got {sum(counts)}",
            )
            # The entry is never corrupted to None: promotion moved the pending
            # value into prefix/original_hashes atomically.
            self.assertEqual(entry["prefix"], sentinel_prefix,
                             f"round {r}: prefix corrupted to {entry['prefix']!r}")
            self.assertEqual(entry["original_hashes"], sentinel_hashes,
                             f"round {r}: original_hashes corrupted to {entry['original_hashes']!r}")
            self.assertIsNone(entry["pending"], f"round {r}: pending not cleared")
            self.assertIsNone(entry["pending_hashes"], f"round {r}: pending_hashes not cleared")


if __name__ == "__main__":
    unittest.main()
