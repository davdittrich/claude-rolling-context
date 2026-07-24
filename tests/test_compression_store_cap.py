"""CompressionStore bound: cap + LRU eviction, and _debug_messages retention gating.

Two failure modes this proves:
1. Unbounded growth — CompressionStore.add()/try_begin_compression() appended
   entries forever; only removed on "nothing to compress" or "no longer
   helps". Stale entries whose hashes never recur pinned memory + made every
   find_match() scan O(entries) forever. Fix: cap entries (env
   ROLLING_CONTEXT_STORE_MAX, default 32), evicting the oldest entry on
   insert once over cap.
2. Unbounded retention per entry — entry["_debug_messages"] pinned the WHOLE
   compressed-away original message list forever, for every entry, by
   default. Fix: gate behind ROLLING_CONTEXT_DEBUG_MESSAGES (default off)
   and cap retained length when on.

Eviction must integrate with the concurrency work in try_begin_compression():
reuses the SAME self._lock (no second lock) and must NEVER evict an entry
that is in_progress OR whose background compression thread is still alive,
even if that entry is the oldest in the store.

Run: python3 -m unittest discover -s tests
"""
import importlib
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import server  # noqa: E402
from server import CompressionStore  # noqa: E402


class StoreCapTest(unittest.TestCase):
    def test_cap_enforced_on_add(self):
        store = CompressionStore(max_entries=5)
        for _ in range(10):
            store.add()
        self.assertLessEqual(len(store.compressions), 5)

    def test_cap_enforced_on_try_begin_compression(self):
        store = CompressionStore(max_entries=2)
        for _ in range(4):
            entry = store.try_begin_compression()
            self.assertIsNotNone(entry)
            entry["in_progress"] = False  # simulate compression finishing
        self.assertLessEqual(len(store.compressions), 2)

    def test_oldest_evicted_first(self):
        store = CompressionStore(max_entries=3)
        entries = [store.add() for _ in range(3)]
        for i, e in enumerate(entries):
            e["_marker"] = i  # tag insertion order for identification
        store.add()  # 4th insert should push the store over cap by one
        remaining = [e.get("_marker") for e in store.compressions if "_marker" in e]
        self.assertNotIn(0, remaining, "oldest entry (marker 0) should have been evicted")
        self.assertIn(1, remaining)
        self.assertIn(2, remaining)

    def test_in_progress_entry_never_evicted(self):
        store = CompressionStore(max_entries=2)
        active = store.try_begin_compression()  # oldest entry, still reserved
        self.assertIsNotNone(active)
        for _ in range(5):
            store.add()
        self.assertIn(active, store.compressions)

    def test_live_thread_entry_never_evicted(self):
        # in_progress is cleared in a `finally` while the thread may still be
        # winding down — eviction must also honor thread.is_alive(), not just
        # the in_progress flag, or a live compression's entry could vanish
        # out from under it.
        store = CompressionStore(max_entries=2)
        entry = store.try_begin_compression()
        release = threading.Event()

        def worker():
            release.wait(5)

        t = threading.Thread(target=worker)
        entry["thread"] = t
        t.start()
        entry["in_progress"] = False
        try:
            for _ in range(5):
                store.add()
            self.assertIn(entry, store.compressions)
        finally:
            release.set()
            t.join()

    def test_default_cap_is_32(self):
        store = CompressionStore()
        self.assertEqual(store._max_entries, 32)


class DebugMessagesGatingTest(unittest.TestCase):
    """entry["_debug_messages"] must be gated off by default and capped when on."""

    def setUp(self):
        self._saved = os.environ.get("ROLLING_CONTEXT_DEBUG_MESSAGES")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("ROLLING_CONTEXT_DEBUG_MESSAGES", None)
        else:
            os.environ["ROLLING_CONTEXT_DEBUG_MESSAGES"] = self._saved
        importlib.reload(server)

    def _run_compression(self, n_messages, tail_verbatim=2):
        messages = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg{i}"}
            for i in range(n_messages)
        ]
        entry = server.store._new_entry()
        fake_compressed = (
            [{"role": "user", "content": "summary"}, {"role": "assistant", "content": "ack"}]
            + messages[-tail_verbatim:]
        )
        orig_compress = server.compressor.compress
        server.compressor.compress = lambda *a, **kw: fake_compressed
        try:
            server._do_background_compression(entry, messages, {})
        finally:
            server.compressor.compress = orig_compress
        expected_summarized = messages[: n_messages - tail_verbatim]
        return entry, expected_summarized

    def test_debug_messages_absent_by_default(self):
        os.environ.pop("ROLLING_CONTEXT_DEBUG_MESSAGES", None)
        importlib.reload(server)
        entry, _ = self._run_compression(n_messages=6)
        self.assertFalse(entry.get("_debug_messages"))

    def test_debug_messages_present_when_enabled(self):
        os.environ["ROLLING_CONTEXT_DEBUG_MESSAGES"] = "1"
        importlib.reload(server)
        entry, expected_summarized = self._run_compression(n_messages=6)
        self.assertEqual(entry.get("_debug_messages"), expected_summarized)

    def test_debug_messages_capped_when_enabled(self):
        os.environ["ROLLING_CONTEXT_DEBUG_MESSAGES"] = "1"
        importlib.reload(server)
        entry, expected_summarized = self._run_compression(n_messages=120)
        self.assertGreater(len(expected_summarized), server.DEBUG_MESSAGES_CAP)
        retained = entry.get("_debug_messages") or []
        self.assertLessEqual(len(retained), server.DEBUG_MESSAGES_CAP)


if __name__ == "__main__":
    unittest.main()
