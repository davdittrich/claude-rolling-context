# Bounded Tiered-Decay Rolling Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the append-only rolling-summary contract (which saturates the 16K output cap and truncates the *newest* material) with oldest-first tiered decay plus a deterministic code guard, so summary size self-stabilizes below a ceiling for unbounded session length.

**Architecture:** No new tier engine — the summary is already sectioned markdown (`## Active Goal / ## Timeline / ## Key Details ...`) that the model maintains each compression. Change the prompt contract on the one section that grows (`## Timeline`) from "copy forward verbatim, never drop" to "merge and abstract the OLDEST entries when nearing budget," protect an Invariants tier (`Active Goal` + user constraints + `Key Details`) as never-decay, and add a code-enforced backstop: capture the API `stop_reason`, and on truncation-or-over-ceiling run one recursive condense pass. State stays entirely in the summary text — stateless proxy unchanged.

**Tech Stack:** Python 3 standard library only (no pip deps). `unittest` test suite run via `python3 -m unittest discover -s tests`. Target file `proxy/compressor.py`.

## Global Constraints

- **Python stdlib only** — no new third-party dependencies, no vector DB, no embeddings.
- **Tests are `unittest`**, class-based, run with `python3 -m unittest discover -s tests`. Test files prepend `sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))` then `import compressor`.
- **Native path first.** The deterministic guard lands on `_summarize_native` only. The identical guard on `_summarize_flattened` is a deferred fast-follow (NOT in this plan). The prompt-contract change (Task 5) applies to both paths.
- **Budgets:** soft target **16K tokens** (prompt guidance, hardcoded — no new env var), hard ceiling **20000** (`max_tokens`, raised from 16000).
- **Preserve prompt-cache:** never change the `messages[:cut]` span native sends. The guard operates only on the *returned summary text*, after generation.
- **Do not touch** the 100K trigger or the blend keep policy (`_find_keep_index`, `keep_turns`/`keep_floor`) — both locked.
- **Commits:** Conventional Commits, no AI attribution/co-author trailer.

---

### Task 1: Extract SSE parse helper that also returns `stop_reason`

The native summarizer's SSE loop (`_summarize_native`, compressor.py:463-489) accumulates text but ignores `message_delta`, which carries `delta.stop_reason`. Extract the loop into a reusable helper that returns `(text, stop_reason)` so both the main call and the recursive condense pass (Task 3) can read whether generation was truncated.

**Files:**
- Modify: `proxy/compressor.py` (add method `_parse_summary_sse`; refactor `_summarize_native` body at :463-489 to call it)
- Test: `tests/test_sse_stop_reason.py` (create)

**Interfaces:**
- Produces: `RollingCompressor._parse_summary_sse(self, resp_body: bytes) -> tuple[str, str | None]` — returns `(summary_text, stop_reason)`. Raises `RuntimeError` on a stream `error` event or empty text (same as today). Logs `message_start` usage as before.

- [ ] **Step 1: Write the failing test**

Create `tests/test_sse_stop_reason.py`:

```python
"""Task 1: _parse_summary_sse returns (text, stop_reason) and captures
message_delta.stop_reason from the native SSE stream.

Run: python3 -m unittest tests.test_sse_stop_reason -v
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import compressor  # noqa: E402


def _sse(*events: dict) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


class ParseSummarySseTest(unittest.TestCase):
    def setUp(self):
        self.comp = compressor.RollingCompressor()

    def test_captures_max_tokens_stop_reason(self):
        body = _sse(
            {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
        )
        text, stop_reason = self.comp._parse_summary_sse(body)
        self.assertEqual(text, "hello")
        self.assertEqual(stop_reason, "max_tokens")

    def test_end_turn_stop_reason(self):
        body = _sse(
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "done"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        )
        text, stop_reason = self.comp._parse_summary_sse(body)
        self.assertEqual(text, "done")
        self.assertEqual(stop_reason, "end_turn")

    def test_empty_text_raises(self):
        body = _sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
        with self.assertRaises(RuntimeError):
            self.comp._parse_summary_sse(body)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_sse_stop_reason -v`
Expected: FAIL with `AttributeError: 'RollingCompressor' object has no attribute '_parse_summary_sse'`.

- [ ] **Step 3: Add the helper and refactor `_summarize_native` to use it**

In `proxy/compressor.py`, add this method just above `_summarize_native` (before the `def _summarize_native` line ~373):

```python
    def _parse_summary_sse(self, resp_body: bytes) -> tuple:
        """Parse a native summarizer SSE body into (text, stop_reason).

        stop_reason is captured from message_delta and is None if absent.
        Raises RuntimeError on a stream error event or empty text.
        """
        parts = []
        stop_reason = None
        for line in resp_body.decode("utf-8", errors="replace").split("\n"):
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            evt = data.get("type", "")
            if evt == "message_start":
                usage = data.get("message", {}).get("usage", {})
                log.info(
                    f"Native compaction usage: input={usage.get('input_tokens', 0):,} "
                    f"cache_read={usage.get('cache_read_input_tokens', 0):,} "
                    f"cache_write={usage.get('cache_creation_input_tokens', 0):,}"
                )
            elif evt == "content_block_delta":
                delta = data.get("delta", {})
                if delta.get("type") == "text_delta":
                    parts.append(delta.get("text", ""))
            elif evt == "message_delta":
                sr = data.get("delta", {}).get("stop_reason")
                if sr is not None:
                    stop_reason = sr
            elif evt == "error":
                raise RuntimeError(f"Summarization stream error: {json.dumps(data)[:500]}")
        summary = "".join(parts).strip()
        if not summary:
            snippet = resp_body.decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"Summarization returned empty text; response starts: {snippet}")
        return summary, stop_reason
```

Then replace the inline parse block in `_summarize_native` (currently compressor.py:463-489, from `parts = []` through `return summary`) with:

```python
        summary, _stop_reason = self._parse_summary_sse(resp_body)
        return summary
```

(The guard that consumes `_stop_reason` is wired in Task 4; leave the underscore for now.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_sse_stop_reason -v`
Expected: 3 PASS.
Run: `python3 -m unittest discover -s tests`
Expected: full suite still green (refactor is behavior-preserving for `_summarize_native`).

- [ ] **Step 5: Commit**

```bash
git add proxy/compressor.py tests/test_sse_stop_reason.py
git commit -m "refactor(compressor): extract _parse_summary_sse returning stop_reason"
```

---

### Task 2: Teach the fake summarizer to emit `stop_reason` and a reply sequence

`tests/_fakes.py:FakeSummarizerConn` emits a single `content_block_delta` and no `message_delta`. Tasks 3–4 need it to (a) emit a chosen `stop_reason`, and (b) return *different* bodies on successive `getresponse()` calls (the recursive condense pass opens a second connection). Extend it without breaking existing callers (default behavior unchanged).

**Files:**
- Modify: `tests/_fakes.py:FakeSummarizerConn`
- Test: `tests/test_fakes_stop_reason.py` (create)

**Interfaces:**
- Produces: `FakeSummarizerConn(reply_text="summary", capture=False, stop_reason=None, replies=None)`. When `replies` (a list of `{"text": str, "stop_reason": str|None}`) is given, successive `getresponse()` calls consume it in order; otherwise every call emits `reply_text`+`stop_reason`. `.bodies` lists every captured outgoing JSON body (when `capture=True`); `.last_body` remains the most recent (back-compat).

- [ ] **Step 1: Write the failing test**

Create `tests/test_fakes_stop_reason.py`:

```python
"""Task 2: FakeSummarizerConn emits stop_reason and a reply sequence.

Run: python3 -m unittest tests.test_fakes_stop_reason -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import compressor  # noqa: E402
sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeSummarizerConn  # noqa: E402


class FakeStopReasonTest(unittest.TestCase):
    def test_single_reply_with_stop_reason(self):
        conn = FakeSummarizerConn(reply_text="hi", stop_reason="max_tokens")
        body = conn.getresponse().read()
        text, sr = compressor.RollingCompressor()._parse_summary_sse(body)
        self.assertEqual(text, "hi")
        self.assertEqual(sr, "max_tokens")

    def test_reply_sequence_consumed_in_order(self):
        conn = FakeSummarizerConn(replies=[
            {"text": "first", "stop_reason": "max_tokens"},
            {"text": "second", "stop_reason": "end_turn"},
        ])
        comp = compressor.RollingCompressor()
        t1, s1 = comp._parse_summary_sse(conn.getresponse().read())
        t2, s2 = comp._parse_summary_sse(conn.getresponse().read())
        self.assertEqual((t1, s1), ("first", "max_tokens"))
        self.assertEqual((t2, s2), ("second", "end_turn"))

    def test_default_behavior_unchanged(self):
        conn = FakeSummarizerConn()
        text, sr = compressor.RollingCompressor()._parse_summary_sse(conn.getresponse().read())
        self.assertEqual(text, "summary")
        self.assertIsNone(sr)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_fakes_stop_reason -v`
Expected: FAIL — `test_single_reply_with_stop_reason` gets `sr is None` (no message_delta emitted yet); `test_reply_sequence_consumed_in_order` fails (both calls return "summary").

- [ ] **Step 3: Extend `FakeSummarizerConn`**

In `tests/_fakes.py`, replace the `FakeSummarizerConn` class body with:

```python
class FakeSummarizerConn:
    """Fake `_summarizer_conn()` return value. By default every
    getresponse() emits a content_block_delta carrying `reply_text` plus a
    message_delta carrying `stop_reason` (None => no message_delta). Pass
    `replies=[{"text":..., "stop_reason":...}, ...]` to return different
    bodies on successive getresponse() calls (consumed in order; the last
    entry repeats once exhausted). `capture` records outgoing JSON bodies
    on `.bodies` (list) and `.last_body` (most recent)."""

    def __init__(self, reply_text: str = "summary", capture: bool = False,
                 stop_reason=None, replies=None):
        self.last_body = None
        self.bodies = []
        self._capture = capture
        if replies is not None:
            self._replies = list(replies)
        else:
            self._replies = [{"text": reply_text, "stop_reason": stop_reason}]
        self._idx = 0

    def request(self, method, path, body=None, headers=None):
        if self._capture:
            parsed = json.loads(body)
            self.bodies.append(parsed)
            self.last_body = parsed

    def getresponse(self):
        i = min(self._idx, len(self._replies) - 1)
        self._idx += 1
        reply = self._replies[i]
        events = [{
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": reply["text"]},
        }]
        if reply.get("stop_reason") is not None:
            events.append({
                "type": "message_delta",
                "delta": {"stop_reason": reply["stop_reason"]},
            })
        sse = "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()
        return FakeResponse(sse)

    def close(self):
        pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_fakes_stop_reason -v`
Expected: 3 PASS.
Run: `python3 -m unittest discover -s tests`
Expected: full suite green (existing callers use defaults; `.last_body` preserved).

- [ ] **Step 5: Commit**

```bash
git add tests/_fakes.py tests/test_fakes_stop_reason.py
git commit -m "test(fakes): emit stop_reason and reply sequences from FakeSummarizerConn"
```

---

### Task 3: Add the condense prompt and `_condense_summary` helper

The recursive backstop re-summarizes an over-budget summary. Add a standalone `CONDENSE_PROMPT` and a helper that sends `CONDENSE_PROMPT + summary_text` to the summarizer and returns the condensed text. It reuses `_parse_summary_sse` (Task 1) and the session model (no cache dependency — this is a rare guard path).

**Files:**
- Modify: `proxy/compressor.py` (add `CONDENSE_PROMPT` constant near the other prompts ~:159; add method `_condense_summary`)
- Test: `tests/test_condense_summary.py` (create)

**Interfaces:**
- Consumes: `_parse_summary_sse` (Task 1), `_summarizer_conn`, `_clean_headers`, `_join_path`, `_SUMMARIZER_PATH`, `_summarizer_conn` (all existing module-level helpers used by `_summarize_native`).
- Produces: `RollingCompressor._condense_summary(self, summary_text: str, auth_headers: dict, model: str) -> str` — returns condensed summary text (the first element of `_parse_summary_sse`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_condense_summary.py`:

```python
"""Task 3: _condense_summary sends CONDENSE_PROMPT + summary and returns
the condensed text.

Run: python3 -m unittest tests.test_condense_summary -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import compressor  # noqa: E402
sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeSummarizerConn  # noqa: E402


class CondenseSummaryTest(unittest.TestCase):
    def setUp(self):
        self._real = compressor._summarizer_conn
        self._fake = FakeSummarizerConn(reply_text="CONDENSED", capture=True)
        compressor._summarizer_conn = lambda timeout=600: self._fake

    def tearDown(self):
        compressor._summarizer_conn = self._real

    def test_returns_condensed_text(self):
        comp = compressor.RollingCompressor()
        out = comp._condense_summary("OVERLONG SUMMARY TEXT", auth_headers={},
                                     model="claude-sonnet-4-5-20250929")
        self.assertEqual(out, "CONDENSED")

    def test_sends_condense_prompt_and_summary(self):
        comp = compressor.RollingCompressor()
        comp._condense_summary("UNIQUE_SUMMARY_MARKER", auth_headers={},
                               model="claude-sonnet-4-5-20250929")
        sent = self._fake.last_body
        blob = "".join(
            b.get("text", "") if isinstance(b, dict) else b
            for m in sent["messages"]
            for b in ([m["content"]] if isinstance(m["content"], str) else m["content"])
        )
        self.assertIn("UNIQUE_SUMMARY_MARKER", blob)
        self.assertIn("16,000", compressor.CONDENSE_PROMPT)
        self.assertEqual(sent["max_tokens"], 20000)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_condense_summary -v`
Expected: FAIL — `AttributeError: ... has no attribute '_condense_summary'` (and `CONDENSE_PROMPT` undefined).

- [ ] **Step 3: Add the constant and helper**

In `proxy/compressor.py`, add after the `SUMMARIZE_PROMPT` definition (~:159):

```python
CONDENSE_PROMPT = """The text below is a rolling conversation summary that exceeded its size budget. Rewrite it to fit within 16,000 tokens.

Preserve at full fidelity, never dropping: the ## Active Goal section, any stated user constraints or rules, and the ## Key Details section.
Compress the OLDEST ## Timeline entries by merging adjacent steps into denser milestone bullets. Never drop the newest entries.
Keep the same section headings and markdown structure. Output ONLY the rewritten summary, nothing else.

SUMMARY TO CONDENSE:
"""
```

Add this method just below `_parse_summary_sse` (from Task 1):

```python
    def _condense_summary(self, summary_text: str, auth_headers: dict, model: str) -> str:
        """Recursive backstop: re-summarize an over-budget summary under the
        soft target, preserving invariants and folding the oldest Timeline.
        Uses a standalone request (no cache dependency — rare guard path)."""
        body = {
            "model": model,
            "max_tokens": 20000,
            "stream": True,
            "messages": [{"role": "user", "content": CONDENSE_PROMPT + summary_text}],
        }
        req_body = json.dumps(body).encode()
        headers = _clean_headers(auth_headers)
        headers["content-length"] = str(len(req_body))
        headers["accept-encoding"] = "identity"
        summarizer_path = _join_path(_SUMMARIZER_PATH, "/v1/messages")
        log.info(f"Summary over budget -> condense pass ({len(summary_text):,} chars)")
        conn = _summarizer_conn()
        conn.request("POST", summarizer_path, body=req_body, headers=headers)
        resp = conn.getresponse()
        resp_body = resp.read()
        conn.close()
        if resp_body[:2] == b"\x1f\x8b":
            resp_body = gzip.decompress(resp_body)
        text, _sr = self._parse_summary_sse(resp_body)
        return text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_condense_summary -v`
Expected: 2 PASS.
Run: `python3 -m unittest discover -s tests`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add proxy/compressor.py tests/test_condense_summary.py
git commit -m "feat(compressor): add CONDENSE_PROMPT and _condense_summary backstop"
```

---

### Task 4: Wire the guard into `_summarize_native` and raise the native cap to 20000

Consume the `stop_reason` from Task 1. Raise `max_tokens` 16000→20000. When generation was truncated (`stop_reason == "max_tokens"`) or the returned summary exceeds the hard ceiling by measured size, run one `_condense_summary` pass and use its output.

**Files:**
- Modify: `proxy/compressor.py:_summarize_native` — `max_tokens` (:421) 16000→20000; the return path (from Task 1's `summary, _stop_reason = ...`)
- Test: `tests/test_summary_decay_guard.py` (create)

**Interfaces:**
- Consumes: `_parse_summary_sse` (Task 1), `_condense_summary` (Task 3).
- Module constant added: `HARD_CEILING_TOKENS = 20000`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_summary_decay_guard.py`:

```python
"""Task 4: native guard runs exactly one condense pass on truncation or
over-ceiling, and leaves normal summaries untouched.

Run: python3 -m unittest tests.test_summary_decay_guard -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import compressor  # noqa: E402
sys.path.insert(0, os.path.dirname(__file__))
from _fakes import FakeSummarizerConn  # noqa: E402

PAYLOAD = {"model": "claude-sonnet-4-5-20250929"}
MESSAGES = [
    {"role": "user", "content": "u1 " * 50},
    {"role": "assistant", "content": "a1 " * 50},
    {"role": "user", "content": "u2 (recent)"},
]


class DecayGuardTest(unittest.TestCase):
    def _patch(self, fake):
        self._real = compressor._summarizer_conn
        compressor._summarizer_conn = lambda timeout=600: fake
        self.addCleanup(lambda: setattr(compressor, "_summarizer_conn", self._real))

    def test_truncation_triggers_one_condense_pass(self):
        fake = FakeSummarizerConn(replies=[
            {"text": "TRUNCATED SUMMARY", "stop_reason": "max_tokens"},
            {"text": "CONDENSED SUMMARY", "stop_reason": "end_turn"},
        ], capture=True)
        self._patch(fake)
        comp = compressor.RollingCompressor(keep_floor=1, keep_turns=1)
        out = comp._summarize_native(PAYLOAD, MESSAGES, cut=2, auth_headers={})
        self.assertEqual(out, "CONDENSED SUMMARY")
        self.assertEqual(len(fake.bodies), 2)  # main + one condense

    def test_over_ceiling_by_size_triggers_condense(self):
        huge = "X" * (compressor.HARD_CEILING_TOKENS * 4 + 10)
        fake = FakeSummarizerConn(replies=[
            {"text": huge, "stop_reason": "end_turn"},
            {"text": "CONDENSED", "stop_reason": "end_turn"},
        ], capture=True)
        self._patch(fake)
        comp = compressor.RollingCompressor(keep_floor=1, keep_turns=1)
        out = comp._summarize_native(PAYLOAD, MESSAGES, cut=2, auth_headers={})
        self.assertEqual(out, "CONDENSED")
        self.assertEqual(len(fake.bodies), 2)

    def test_normal_summary_no_condense(self):
        fake = FakeSummarizerConn(reply_text="FINE SUMMARY",
                                  stop_reason="end_turn", capture=True)
        self._patch(fake)
        comp = compressor.RollingCompressor(keep_floor=1, keep_turns=1)
        out = comp._summarize_native(PAYLOAD, MESSAGES, cut=2, auth_headers={})
        self.assertEqual(out, "FINE SUMMARY")
        self.assertEqual(len(fake.bodies), 1)  # main only, no condense

    def test_native_cap_is_20000(self):
        fake = FakeSummarizerConn(reply_text="ok", stop_reason="end_turn", capture=True)
        self._patch(fake)
        comp = compressor.RollingCompressor(keep_floor=1, keep_turns=1)
        comp._summarize_native(PAYLOAD, MESSAGES, cut=2, auth_headers={})
        self.assertEqual(fake.bodies[0]["max_tokens"], 20000)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_summary_decay_guard -v`
Expected: FAIL — `test_native_cap_is_20000` sees 16000; `test_truncation_*`/`test_over_ceiling_*` see only 1 body (no guard) and `out` is the raw truncated/huge text; `HARD_CEILING_TOKENS` undefined.

- [ ] **Step 3: Implement the guard**

In `proxy/compressor.py`, add near the other module constants (~:100, by `SUMMARY_MARKER`):

```python
HARD_CEILING_TOKENS = 20000  # native summary hard ceiling (max_tokens)
```

In `_summarize_native`, change the cap line (currently `max_tokens = 16000`, :421) to:

```python
        max_tokens = HARD_CEILING_TOKENS
```

Replace Task 1's return stub (`summary, _stop_reason = self._parse_summary_sse(resp_body)` / `return summary`) with:

```python
        summary, stop_reason = self._parse_summary_sse(resp_body)
        over_ceiling = len(summary) > HARD_CEILING_TOKENS * 4  # ~4 chars/token estimate
        if stop_reason == "max_tokens" or over_ceiling:
            log.info(
                f"Summary guard fired (stop_reason={stop_reason}, "
                f"chars={len(summary):,}) -> condense pass"
            )
            summary = self._condense_summary(summary, auth_headers, model)
        return summary
```

(`model` is already in scope in `_summarize_native` from `model = payload.get("model", LEGACY_DEFAULT_MODEL)` at :420.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_summary_decay_guard -v`
Expected: 4 PASS.
Run: `python3 -m unittest discover -s tests`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add proxy/compressor.py tests/test_summary_decay_guard.py
git commit -m "feat(compressor): native summary guard — condense on truncation or over-ceiling"
```

---

### Task 5: Flip the tier contract in the three prompts and update the carry-forward test

Replace the append-only contract with oldest-first tiered decay in `SUMMARY_RULES` (:103-132), `NATIVE_COMPACT_PROMPT` (:143), and the flattened existing-summary section (`compress`, :617-620). Preserve the Invariants tier. `tests/test_native_summary_carry_forward.py` currently asserts the *old* contract (`"VERBATIM"`, `"APPEND"`, `"never paraphrase"`, `"drop any entry"`) and MUST be updated to assert the new one — while keeping its structural guarantees (span byte-identical, exactly one summary block).

**Files:**
- Modify: `proxy/compressor.py` — `SUMMARY_RULES` (:103-132), `NATIVE_COMPACT_PROMPT` (:143), `compress` existing-summary section (:616-621)
- Modify: `tests/test_native_summary_carry_forward.py` — the two prompt-text assertion methods
- Test: `tests/test_decay_contract.py` (create)

**Interfaces:**
- Produces: prompts contain the decay instruction (substring `"OLDEST"` and `"Timeline"`) and the invariant clause (substring `"Active Goal"` and `"never"`), and no longer contain `"copy it forward exactly"`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_decay_contract.py`:

```python
"""Task 5: prompts mandate oldest-first decay + invariant preservation and
drop the append-only language.

Run: python3 -m unittest tests.test_decay_contract -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))
import compressor  # noqa: E402


class DecayContractTest(unittest.TestCase):
    def test_native_prompt_mandates_oldest_first_decay(self):
        p = compressor.NATIVE_COMPACT_PROMPT
        self.assertIn("OLDEST", p)
        self.assertIn("Timeline", p)

    def test_native_prompt_preserves_invariants(self):
        p = compressor.NATIVE_COMPACT_PROMPT
        self.assertIn("Active Goal", p)
        self.assertIn("Key Details", p)

    def test_append_only_language_removed(self):
        p = compressor.NATIVE_COMPACT_PROMPT
        self.assertNotIn("copy it forward exactly", p)

    def test_summary_rules_declare_budget(self):
        self.assertIn("16,000", compressor.SUMMARY_RULES)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_decay_contract -v`
Expected: FAIL — current prompt has neither `"OLDEST"` nor the budget and still contains `"copy it forward exactly"`.

- [ ] **Step 3: Rewrite `SUMMARY_RULES` budget/decay lines**

In `proxy/compressor.py`, append these lines to the end of the `SUMMARY_RULES` string (immediately after the `## Key Details` line at :132, inside the triple-quoted string, before the closing `"""`):

```

BUDGET & DECAY:
- Keep the whole summary within ~16,000 tokens.
- INVARIANTS — never condense or drop: the ## Active Goal section, any stated user constraints (do/don't rules), and the ## Key Details section.
- The ## Timeline is the only section that may shrink. Keep the most recent ~15-20 steps detailed. As the summary approaches its budget, MERGE the OLDEST Timeline steps into denser milestone bullets rather than dropping the newest.
```

- [ ] **Step 4: Rewrite `NATIVE_COMPACT_PROMPT`'s carry-forward paragraph**

In `proxy/compressor.py`, replace the paragraph at :143 (the one beginning `If the conversation begins with a {SUMMARY_MARKER} block`) with:

```python
If the conversation begins with a {SUMMARY_MARKER} block from an earlier compression, carry it forward and extend it. Preserve its ## Active Goal, stated user constraints, and ## Key Details at full fidelity — never condense or drop them. Keep recent ## Timeline entries detailed. As the combined summary approaches its ~16,000 token budget, MERGE the OLDEST Timeline entries into denser milestone bullets rather than dropping the newest events. Do not truncate the most recent entries.
```

- [ ] **Step 5: Rewrite the flattened existing-summary section**

In `proxy/compressor.py:compress` (:616-621), replace the `existing_section = (...)` assignment with:

```python
                existing_section = (
                    "EXISTING ROLLING SUMMARY FROM PREVIOUS COMPRESSIONS "
                    "(carry it forward and extend it; preserve ## Active Goal, "
                    "user constraints, and ## Key Details at full fidelity; as "
                    "the summary nears ~16,000 tokens, merge the OLDEST Timeline "
                    "entries into denser bullets rather than dropping the "
                    "newest):\n"
                    f"{existing_summary}\n\n"
                )
```

- [ ] **Step 6: Update the carry-forward test's prompt assertions**

In `tests/test_native_summary_carry_forward.py`, replace the two methods in `NativeCompactPromptTextTest` (`test_mandates_verbatim_carry_forward`, `test_forbids_paraphrasing_earlier_entries`) with:

```python
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
```

Also update the assertion inside `test_prompt_sent_carries_prior_summary_and_instruction_verbatim` (currently `self.assertIn("VERBATIM", sent_messages[-1]["content"])`) to:

```python
        self.assertIn("OLDEST", sent_messages[-1]["content"])
```

(The byte-identical span assertions and the "exactly one summary block" test are unchanged — they still hold and must stay.)

- [ ] **Step 7: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_decay_contract tests.test_native_summary_carry_forward -v`
Expected: all PASS.
Run: `python3 -m unittest discover -s tests`
Expected: full suite green.

- [ ] **Step 8: Commit**

```bash
git add proxy/compressor.py tests/test_decay_contract.py tests/test_native_summary_carry_forward.py
git commit -m "feat(compressor): oldest-first tiered decay contract replaces append-only"
```

---

### Task 6: Raise the flattened cap and document the new defaults

Bring the flattened path's `max_tokens` to 20000 for consistency (its recursive guard is a deferred fast-follow, not in this plan). Update the README defaults note.

**Files:**
- Modify: `proxy/compressor.py:_summarize_flattened` (:496)
- Modify: `README.md` (defaults/behavior note)
- Test: `tests/test_flattened_cap.py` (create)

**Interfaces:** none new.

- [ ] **Step 1: Write the failing test**

Create `tests/test_flattened_cap.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_flattened_cap -v`
Expected: FAIL — source still has `summary_max_tokens = 16000`.

- [ ] **Step 3: Raise the flattened cap**

In `proxy/compressor.py:_summarize_flattened` (:496), change:

```python
        summary_max_tokens = 20000
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_flattened_cap -v`
Expected: PASS.

- [ ] **Step 5: Update the README**

In `README.md`, find the summarization/behavior description and add (or amend the existing cap note) a line stating: the rolling summary uses **oldest-first tiered decay** with a soft target of ~16K tokens and a hard ceiling of 20K (`max_tokens`); on truncation or over-ceiling the native path runs one deterministic condense pass so the newest material is never the casualty. If a defaults table lists `max_tokens`/16000, update it to 20000.

- [ ] **Step 6: Run the full suite and commit**

Run: `python3 -m unittest discover -s tests`
Expected: full suite green.

```bash
git add proxy/compressor.py README.md tests/test_flattened_cap.py
git commit -m "feat(compressor): raise flattened cap to 20000; document tiered decay"
```

---

## Manual pre-ship verification (not a unit test)

Whether the model *actually* decays oldest-first is a model-behavior claim unprovable with mocks (same framing as the existing `test_native_summary_carry_forward` docstring). Before shipping, run one long real session through repeated compressions against a live backend and confirm from the proxy log that (a) summary size stabilizes at/below ~16K tokens across compressions rather than growing with turn count, and (b) the "Summary guard fired" line appears rarely. The observability log lines added in Tasks 3–4 make this measurable in production.

## Self-Review

**Spec coverage:**
- Spec §3.2 tier contract (Invariants never-decay, oldest-first Timeline, budgets) → Task 5 (prompts) + Task 5 Step 3 (`SUMMARY_RULES` budget/decay).
- Spec §3.3 guard (capture stop_reason; recursive condense on truncation/over-ceiling; native-first; observability) → Tasks 1 (stop_reason), 3 (condense), 4 (wire + native-first). Flattened guard correctly *excluded* (deferred fast-follow per §8).
- Spec §3.4 params (16K soft hardcoded, 20K hard, 1 retry, no env var) → Task 3 (CONDENSE_PROMPT "16,000"), Task 4 (`HARD_CEILING_TOKENS=20000`, single pass), Task 6 (flattened 20000). No env var added ✓.
- Spec §5 testing (contract, guard, stop_reason parse, saturation-manual) → Tasks 1–6 tests + Manual pre-ship section.
- Spec §6 rollout (README note, watch counter) → Task 6 Step 5 + observability logs.

**Placeholder scan:** No TBD/TODO/"add error handling"/"similar to Task N". Every code step shows full code. ✓

**Type consistency:** `_parse_summary_sse(resp_body) -> (text, stop_reason)` defined Task 1, consumed identically in Tasks 3 (`text, _sr = ...`) and 4 (`summary, stop_reason = ...`). `_condense_summary(summary_text, auth_headers, model)` defined Task 3, called with the same signature in Task 4. `FakeSummarizerConn(..., stop_reason=, replies=)` defined Task 2, used in Tasks 3–4. `HARD_CEILING_TOKENS` defined Task 4, referenced in its own tests. Consistent. ✓
