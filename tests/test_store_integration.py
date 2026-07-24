"""Integration + regression: the nine hardening tickets COMPOSE on the shared store.

Per-ticket unit tests prove each fix in isolation. This suite proves they
hold together on the ONE shared data structure they all touch —
CompressionStore._compressions — plus the promote->match->inject contract
that rides on top of it.

Two things are exercised:

1. Store under concurrency (Gemini-e86.2 atomic reserve + Gemini-e86.6 cap/LRU
   eviction) WHILE a reader thread scans the same list via find_match:
     - no exception escapes any thread,
     - in-progress / live-thread entries are NEVER evicted even at cap,
     - exactly ONE compression is reserved per trigger (no duplicate compression),
     - find_match keeps returning the correct match (right entry, right
       match_end) while the store is heavily mutated concurrently.
   Determinism comes from threading.Barrier / Event, not sleeps.

   Caveat, mutation-tested: the find_match sub-property does NOT prove
   find_match's own `with self._lock:` is load-bearing — with that lock
   deleted, test_find_match_correct_under_concurrent_mutation still passes
   identically. The actual safety mechanism is CompressionStore.remove()
   reassigning self._compressions to a NEW list object (copy-on-write)
   rather than mutating in place, so any in-flight iteration over the old
   list object sees a stable snapshot regardless of whether the reader
   held the lock. See that test's docstring for detail.

2. A promote->match->inject cycle driven at the Python level against the REAL
   CompressionStore and the REAL _hash_messages / find_match: seed a pending
   compression, promote it (pending -> prefix / original_hashes), hash a
   follow-up request that replays the compressed history, and assert find_match
   locates the entry and the compressed prefix would be injected in place of the
   original messages.

docker-compose.e2e.yml at the repo root was preferred but is not runnable in
this environment: docker is unavailable AND its referenced build context
(test/Dockerfile.e2e) does not exist. The Python-level integration test below
is the sanctioned fallback (drives the real store), and is strictly
deterministic.

Run: python3 -m unittest discover -s tests
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import server  # noqa: E402
from server import CompressionStore, _hash_messages  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from _fakes import seed_evictable  # noqa: E402


def _seed_matchable(store, chain_messages, prefix, active=False):
    """Insert an entry that find_match WILL match: real hashes + a prefix.

    active=True marks it in_progress so eviction must skip it (Gemini-e86.6
    invariant). Bypasses try_begin_compression() (creates an empty entry)
    and seed_evictable() (also empty); we need original_hashes populated to
    be matchable, so this builds and appends the entry directly.
    """
    entry = store._new_entry()
    entry["original_hashes"] = _hash_messages(chain_messages)
    entry["prefix"] = list(prefix)
    entry["in_progress"] = bool(active)
    with store._lock:
        store._compressions.append(entry)
    return entry


class StoreConcurrencyEvictionTest(unittest.TestCase):
    """Gemini-e86.2 (lock/atomic begin) x Gemini-e86.6 (cap/LRU) x find_match reader."""

    def test_exactly_one_reserved_per_trigger(self):
        """N threads racing try_begin_compression at a barrier: exactly one wins.

        This is the anti-duplicate-compression invariant. Repeated over many
        rounds; between rounds the winner 'finishes' (clears in_progress and is
        removed) so the next round starts from a clean slate.
        """
        store = CompressionStore(max_entries=64)
        n_threads = 16
        n_rounds = 40
        errors = []

        for _ in range(n_rounds):
            barrier = threading.Barrier(n_threads)
            winners = []
            wlock = threading.Lock()

            def worker():
                try:
                    barrier.wait()
                    entry = store.try_begin_compression()
                    if entry is not None:
                        with wlock:
                            winners.append(entry)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [], f"thread raised: {errors}")
            self.assertEqual(
                len(winners), 1,
                f"exactly one reservation expected per trigger, got {len(winners)}",
            )
            # Winner finishes: clear flag + remove so next round is clean.
            winner = winners[0]
            winner["in_progress"] = False
            store.remove(winner)

    def test_active_entries_never_evicted_under_add_storm(self):
        """Cap-forcing seed_evictable() storm must not evict in_progress /
        live-thread entries. Uses seed_evictable() (not try_begin_compression())
        because pinned_in_progress below makes try_begin_compression() refuse
        for every writer thread; seed_evictable() replicates the deleted
        CompressionStore.add()'s append+evict primitive, which applies
        eviction pressure unconditionally."""
        cap = 4
        store = CompressionStore(max_entries=cap)
        release = threading.Event()

        # Pin one in_progress entry and one live-thread entry (in_progress
        # already cleared, thread still alive — the exact window Gemini-e86.6
        # must honor).
        pinned_in_progress = store.try_begin_compression()
        self.assertIsNotNone(pinned_in_progress)

        pinned_thread_entry = store.try_begin_compression()
        # try_begin refuses while another is in_progress, so reserve manually.
        if pinned_thread_entry is None:
            pinned_thread_entry = store._new_entry()
            with store._lock:
                store._compressions.append(pinned_thread_entry)
        worker_thread = threading.Thread(target=lambda: release.wait(10))
        pinned_thread_entry["thread"] = worker_thread
        worker_thread.start()
        pinned_thread_entry["in_progress"] = False  # cleared, but thread alive

        errors = []
        n_writers = 12
        adds_each = 50
        barrier = threading.Barrier(n_writers)

        def writer():
            try:
                barrier.wait()
                for _ in range(adds_each):
                    seed_evictable(store)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer) for _ in range(n_writers)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            release.set()
            worker_thread.join()

        self.assertEqual(errors, [], f"writer raised: {errors}")
        snapshot = store.compressions
        self.assertIn(pinned_in_progress, snapshot,
                      "in_progress entry was evicted under add storm")
        self.assertIn(pinned_thread_entry, snapshot,
                      "live-thread entry was evicted under add storm")
        # Tight bound, not cap + active_count: seed_evictable() is atomic
        # under store._lock (append THEN _evict_locked in one acquisition),
        # and the 2 pinned actives are < cap, so every single
        # seed_evictable() call evicts back to <= cap before releasing the
        # lock (the just-appended entry is itself non-active and evictable,
        # so the evict loop can always reach cap). By induction over every
        # serialized seed_evictable() call, the invariant
        # len(_compressions) <= cap holds after each one — including the
        # last, i.e. at snapshot time. Verified empirically over 30 runs at
        # this exact bound (no cap+2 slack observed or required).
        self.assertLessEqual(len(snapshot), cap)

    def test_find_match_correct_under_concurrent_mutation(self):
        """find_match returns the correct match (right entry, right match_end)
        while a writer races seed_evictable()/try_begin_compression()/remove()
        against the same store, pre-seeded to a non-trivial size so every scan
        does real work.

        NOT a proof that find_match's own `with self._lock:` is load-bearing:
        mutation-tested with that lock deleted, this test still passes
        identically (see module docstring caveat). The safety relied on here
        is CompressionStore.remove() reassigning self._compressions to a NEW
        list object (copy-on-write) rather than mutating in place, so any
        iteration already bound to the old list object — locked or not — sees
        a stable snapshot. What this test verifies: find_match's matching
        logic stays correct on every one of 500 scans under heavy, realistic
        concurrent structural churn of a many-entry store.
        """
        cap = 64
        store = CompressionStore(max_entries=cap)

        chain_messages = [
            {"role": "user", "content": "who won the 1998 world cup"},
            {"role": "assistant", "content": "France."},
            {"role": "user", "content": "and the host"},
        ]
        prefix = [
            {"role": "user", "content": "[summary of earlier turns]"},
            {"role": "assistant", "content": "acknowledged"},
        ]

        # Decoys: dozens of non-matching entries (distinct hashes) so
        # find_match scans a real list on every call instead of a list of
        # one. cap is sized well above n_decoys + pinned + the writer's
        # transient/leaked entries, so this seeding is never touched by
        # eviction — this test is about matching correctness under churn,
        # not eviction (that's test_active_entries_never_evicted_under_add_storm).
        n_decoys = 40
        for i in range(n_decoys):
            decoy_chain = [
                {"role": "user", "content": f"decoy question #{i}"},
                {"role": "assistant", "content": f"decoy answer #{i}"},
            ]
            decoy_prefix = [{"role": "user", "content": f"[decoy summary #{i}]"}]
            _seed_matchable(store, decoy_chain, decoy_prefix, active=False)

        # Pinned, active, matchable: must survive all eviction and always match.
        pinned = _seed_matchable(store, chain_messages, prefix, active=True)

        follow_up = chain_messages + [{"role": "user", "content": "thanks"}]
        req_hashes = _hash_messages(follow_up)
        expected_end = len(chain_messages)

        errors = []
        stop = threading.Event()
        start = threading.Barrier(2)

        reader_scans = 500
        reader_iters = [0]

        # Reader runs a FIXED number of scans (deterministic overlap); the
        # writer churns the shared list continuously until the reader is done,
        # so every scan races real concurrent mutation over the full,
        # decoy-padded list.
        def reader():
            try:
                start.wait()
                for _ in range(reader_scans):
                    match, end = store.find_match(req_hashes, follow_up)
                    if match is not pinned or end != expected_end:
                        raise AssertionError(
                            f"incorrect match under churn: match_is_pinned="
                            f"{match is pinned} end={end} (want {expected_end})"
                        )
                    reader_iters[0] += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                stop.set()

        def writer():
            try:
                start.wait()
                while not stop.is_set():
                    e = seed_evictable(store)
                    store.try_begin_compression()
                    store.remove(e)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        rt = threading.Thread(target=reader)
        wt = threading.Thread(target=writer)
        rt.start()
        wt.start()
        wt.join()
        rt.join()

        self.assertEqual(errors, [], f"thread raised: {errors}")
        self.assertEqual(reader_iters[0], reader_scans,
                         "reader did not complete all scans")
        self.assertIn(pinned, store.compressions,
                      "active matchable entry was evicted mid-scan")


class PromoteMatchInjectTest(unittest.TestCase):
    """The load-bearing contract: a promoted compression is injected in place of
    the original messages when the request replays that history."""

    def test_promote_then_match_then_inject(self):
        store = CompressionStore(max_entries=8)

        # History that the background compressor summarized away.
        original_messages = [
            {"role": "user", "content": "start of a long conversation"},
            {"role": "assistant", "content": "sure, let's begin"},
            {"role": "user", "content": "here is a lot of detail ..."},
            {"role": "assistant", "content": "understood, noted all of it"},
        ]
        prefix = [
            {"role": "user", "content": "[summary of everything above]"},
            {"role": "assistant", "content": "ok, continuing"},
        ]

        # 1. A background compression finished: it left pending + pending_hashes,
        #    exactly as _do_background_compression does before promotion.
        entry = store._new_entry()
        with store._lock:
            store._compressions.append(entry)
        entry["pending"] = list(prefix)
        entry["pending_hashes"] = _hash_messages(original_messages)

        # 2. Promote — mirrors the POST handler's promote loop verbatim.
        for e in store.compressions:
            if e["pending"] is not None:
                e["prefix"] = e["pending"]
                e["original_hashes"] = e["pending_hashes"]
                e["pending"] = None
                e["pending_hashes"] = None

        self.assertEqual(entry["prefix"], prefix)
        self.assertEqual(entry["original_hashes"], _hash_messages(original_messages))
        self.assertIsNone(entry["pending"])

        # 3. A follow-up request replays the whole history plus a new turn.
        new_turn = [{"role": "user", "content": "next question please"}]
        request_messages = original_messages + new_turn
        req_hashes = _hash_messages(request_messages)

        # 4. Match against the REAL store.find_match.
        match, match_end = store.find_match(req_hashes, request_messages)
        self.assertIs(match, entry, "find_match did not return the promoted entry")
        self.assertEqual(match_end, len(original_messages),
                         "match must end right after the compressed history")

        # 5. Inject — mirrors the handler: replace [0:match_end] with the prefix.
        self.assertTrue(match["prefix"] is not None and match_end > 0)
        merged = match["prefix"] + request_messages[match_end:]

        self.assertEqual(merged, prefix + new_turn,
                         "injected result must be prefix + verbatim tail")
        # The original bulk is gone: none of its hashes survive in the merged set.
        merged_hashes = set(_hash_messages(merged))
        for h in _hash_messages(original_messages):
            self.assertNotIn(h, merged_hashes,
                             "an original (compressed-away) message leaked into merged")
        # And injection is a real reduction.
        self.assertLess(len(merged), len(request_messages))

    def test_no_duplicate_compression_while_one_in_progress(self):
        """Injection contract's precondition: once a compression is reserved,
        a second concurrent trigger cannot start another (Gemini-e86.2)."""
        store = CompressionStore(max_entries=8)
        first = store.try_begin_compression()
        self.assertIsNotNone(first)
        # While `first` is in_progress, every further trigger is refused.
        for _ in range(5):
            self.assertIsNone(store.try_begin_compression())
        first["in_progress"] = False
        # Once cleared, a new reservation is allowed again.
        self.assertIsNotNone(store.try_begin_compression())


if __name__ == "__main__":
    unittest.main()
