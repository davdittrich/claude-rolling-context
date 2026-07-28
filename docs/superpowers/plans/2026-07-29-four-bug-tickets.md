# Four Bug Tickets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close five open bugs on the rolling-context proxy: a daemon-killing parse crash, an import-frozen mode flag, an alert that fails silent, and two behaviours no test constrains.

**Architecture:** Four of the five are small and independent. Task 1 is sequenced first because Task 5's correctness argument depends on it. Total behaviour change across the plan is ~15 lines; the rest is tests.

**Tech Stack:** Python 3 standard library only. `unittest` via `pytest`. No new dependencies.

## Global Constraints

- **Pure stdlib.** `proxy/chain.py` must import cleanly on Windows — no `fcntl`, no third-party imports.
- **Every new test must be mutation-proven.** Break the behaviour, confirm THAT test fails by name, restore the file, prove the restore with sha256. A test never seen failing is not evidence.
- **Verify the mutation landed** (hash or diff the file) before reading a green suite as a test gap. A prior session misread a silent no-op mutation as a missing test.
- **Suite must be green in three environments:** plain; `ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ROLLING_CONTEXT_PORT=8787`; and `ROLLING_CONTEXT_SUMMARIZER_URL=http://127.0.0.1:9999`. The third is currently red with 6 failures and becomes green in Task 2 — that is Task 2's acceptance test.
- **Baseline is 233 passed, 7 subtests** at commit `09ff584`.
- **Tool constraint:** `Read`/`Grep`/`Glob` are denied in this repo. Use `mcp__plugin_context-mode_context-mode__ctx_execute` (python or shell) for all file reading and editing. `Edit` will not work — it requires a prior in-session `Read`. To edit: read in python, `assert text.count(old) == 1`, `text.replace(old, new)`, write back.
- **Commits:** Conventional Commits with the why. No emojis, no AI attribution, no `Co-Authored-By`. Never `--no-verify`.

---

### Task 1: Give current_upstream a single typed failure mode

**Ticket:** Gemini-0p1

**Files:**
- Modify: `proxy/server.py` (the `current_upstream()` body, and `_upstream_error_body` around line 258)
- Test: `tests/test_malformed_upstream.py` (create)

**Interfaces:**
- Consumes: `UpstreamRefused(url, source, reason)` — already exists, already raised with reasons `"loop"` and `"not-loopback"`.
- Produces: a third reason, `"malformed"`. Every existing handler of `UpstreamRefused` covers it automatically — that is the point of the change.

**Why:** Reproduced on master, daemon exits rc=1 and never binds:

```
ROLLING_CONTEXT_UPSTREAM=http://127.0.0.1:abc    -> ValueError: Port could not be cast to integer value as 'abc'
ROLLING_CONTEXT_UPSTREAM=http://[::1             -> ValueError: Invalid IPv6 URL
ROLLING_CONTEXT_UPSTREAM=http://127.0.0.1:99999  -> ValueError: Port out of range 0-65535
```

`current_upstream()` calls `urlparse(raw)` and `parsed.port` unguarded. `_handle_health` and `main()` each catch only `UpstreamRefused` and `chain.UnparseableSettings`, so `ValueError` escapes both. This is the third instance of one class — Task 7 fixed `/health` raising `UnparseableSettings`, Task 10 fixed `main()` raising it. Fix the class: make `current_upstream()` raise exactly one typed error, so no caller needs a third `except`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_malformed_upstream.py`:

```python
"""A malformed upstream must degrade, never kill the daemon (Gemini-0p1).

current_upstream() parsed the URL unguarded, so a bad port or a malformed IPv6
literal raised ValueError out of both _handle_health and main() -- neither of
which catches it. The daemon died before binding its socket.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "proxy"))

import chain  # noqa: E402
import server  # noqa: E402

from _fakes import hermetic_home  # noqa: E402

MALFORMED = [
    "http://127.0.0.1:abc",
    "http://[::1",
    "http://127.0.0.1:99999",
]


class MalformedUpstreamTest(unittest.TestCase):
    def setUp(self):
        self.home = hermetic_home(self)
        server._UPSTREAM_CACHE["stamp"] = None
        server._UPSTREAM_CACHE["value"] = None
        self.addCleanup(lambda: server._UPSTREAM_CACHE.update({"stamp": None, "value": None}))

    def _write_upstream(self, value):
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"env": {"ROLLING_CONTEXT_UPSTREAM": value}}, f)
        server._UPSTREAM_CACHE["stamp"] = None
        server._UPSTREAM_CACHE["value"] = None

    def test_a_malformed_upstream_raises_the_typed_error_not_valueerror(self):
        for value in MALFORMED:
            with self.subTest(value=value):
                self._write_upstream(value)
                with self.assertRaises(server.UpstreamRefused) as caught:
                    server.current_upstream()
                self.assertEqual(caught.exception.reason, "malformed")

    def test_the_error_body_names_the_malformed_value(self):
        self._write_upstream("http://127.0.0.1:abc")
        try:
            server.current_upstream()
        except server.UpstreamRefused as exc:
            body = json.loads(server._upstream_error_body(exc))
        self.assertEqual(body["type"], "error")
        self.assertIn("127.0.0.1:abc", body["error"]["message"])

    def test_the_daemon_still_binds_with_a_malformed_upstream(self):
        home = tempfile.mkdtemp(prefix="malformed-daemon-")
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        with open(os.path.join(home, ".claude", "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"env": {"ROLLING_CONTEXT_UPSTREAM": "http://127.0.0.1:abc"}}, f)
        env = dict(os.environ, HOME=home, ROLLING_CONTEXT_PORT="5607")
        for key in ("ANTHROPIC_BASE_URL", "ROLLING_CONTEXT_UPSTREAM",
                    "ROLLING_CONTEXT_SUMMARIZER_URL"):
            env.pop(key, None)
        proc = subprocess.run([sys.executable, os.path.join(REPO, "proxy", "server.py")],
                              env=env, capture_output=True, text=True, timeout=15)
        # It must not die on a traceback. It may exit for any other reason.
        self.assertNotIn("ValueError", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `python3 -m pytest tests/test_malformed_upstream.py -q`
Expected: FAIL — `ValueError` is raised instead of `UpstreamRefused`, and the daemon subprocess emits a traceback.

- [ ] **Step 3: Make the parse typed**

In `proxy/server.py`, the block currently reads:

```python
    raw = raw or "https://api.anthropic.com"
    parsed = urlparse(raw)
```

and later builds `Upstream(parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), parsed.path or "", source)` after computing `source`.

Restructure so `source` is known before the parse, and the parse is typed. Replace `raw = raw or "https://api.anthropic.com"` and the bare `parsed = urlparse(raw)` with:

```python
    raw = raw or "https://api.anthropic.com"

    if from_env:
        source = "<environment>"
    elif from_file:
        source = chain.user_settings_path()
    else:
        source = "(default)"

    # One typed failure out of this function. Callers already handle
    # UpstreamRefused; a bare ValueError escaping here killed the daemon at
    # startup and turned /health into a traceback.
    try:
        parsed = urlparse(raw)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UpstreamRefused(raw, source, "malformed") from exc
```

Then delete the now-duplicated `if from_env: source = ...` block that followed the D18 check, and change the `Upstream(...)` construction to use the already-computed `port` instead of re-evaluating `parsed.port`.

Leave the D18 loopback refusal exactly where it is, between the parse and the `Upstream(...)` construction.

- [ ] **Step 4: Give the error body a malformed branch**

`_upstream_error_body` branches on `exc.reason` around line 258. Add a branch for `"malformed"` alongside the existing `"loop"` one, wording it so the user can act — name the offending value and the file or environment it came from. Follow the existing branches' phrasing and the D9 shape: the body stays `{"type": "error", "error": {"type": ..., "message": ...}}`.

- [ ] **Step 5: Run the tests**

Run: `python3 -m pytest tests/ -q`
Expected: 236 passed (233 + 3 new), 7 subtests.

- [ ] **Step 6: Mutation-prove**

Revert the `try/except ValueError` to a bare `parsed = urlparse(raw)` (keeping `port` computed inline), run the suite, confirm `test_a_malformed_upstream_raises_the_typed_error_not_valueerror` fails, restore, prove restore with sha256.

- [ ] **Step 7: Commit**

```bash
git add proxy/server.py tests/test_malformed_upstream.py
git commit -m "fix(proxy): give current_upstream one typed failure mode

A malformed ROLLING_CONTEXT_UPSTREAM raised ValueError out of an unguarded
urlparse and parsed.port. Neither _handle_health nor main() catches that, so a
bad port or a malformed IPv6 literal killed the daemon before it bound its
socket and turned /health into a traceback.

Both call sites already handle UpstreamRefused, so raising that instead of
leaking ValueError fixes the class rather than adding a third except clause in
two more places."
```

---

### Task 2: Resolve native mode per call instead of at import

**Ticket:** Gemini-oou

**Files:**
- Modify: `proxy/compressor.py` (the constants around lines 39-50, and `use_native` around line 712)
- Modify: `proxy/server.py` (the import at line 32, `/health` at line 934, startup log at line 1245)
- Modify: `tests/test_native_mode_model.py` (8 assertions and one docstring)

**Interfaces:**
- Produces: `compressor.native_mode()` — returns `bool`, computed fresh from the environment on every call.
- Removes: the module-level `NATIVE_MODE` and `SUMMARIZER_URL_SET` constants.

**Why:** `SUMMARIZER_URL_SET` and `NATIVE_MODE` are computed once at import. The daemon is long-lived and `start-proxy.sh` reuses it, so this is the same freeze class as the upstream bug already fixed, one module over. Today, exporting `ROLLING_CONTEXT_SUMMARIZER_URL` makes 6 tests fail because the flag froze before they could set it.

- [ ] **Step 1: Confirm the 6 failures**

Run: `env ROLLING_CONTEXT_SUMMARIZER_URL=http://127.0.0.1:9999 python3 -m pytest tests/ -q`
Expected: 6 failed — 2 in `test_compress_accounting.py`, 2 in `test_health_last_compression.py`, 2 in `test_native_summary_carry_forward.py`.

- [ ] **Step 2: Replace the constants with a function**

In `proxy/compressor.py`, delete the `SUMMARIZER_URL_SET` assignment (line 39), the `MODEL_SET` assignment (line 49) and the `NATIVE_MODE` assignment (line 50). Keep `SUMMARIZER_API_KEY`, `SUMMARIZER_FORMAT` and `SUMMARIZER_MODEL` — they have other consumers. Add, after them, keeping the existing explanatory comment about why a pinned model disables native mode:

```python
def native_mode():
    """True when the cloned-session-request path is usable, computed fresh.

    A frozen flag is the same defect class as a frozen upstream: this daemon is
    long-lived and start-proxy.sh reuses it, so a value captured at import
    outlives the configuration it described.
    """
    return not (
        os.environ.get("ROLLING_CONTEXT_SUMMARIZER_URL")
        or os.environ.get("ROLLING_CONTEXT_SUMMARIZER_KEY")
        or (os.environ.get("ROLLING_CONTEXT_SUMMARIZER_FORMAT") or "anthropic").lower() != "anthropic"
        or os.environ.get("ROLLING_CONTEXT_MODEL")
    )
```

- [ ] **Step 3: Update the four use sites**

- `proxy/compressor.py` ~line 712: `use_native = NATIVE_MODE and payload is not None` becomes `use_native = native_mode() and payload is not None`.
- `proxy/server.py` line 32: drop `NATIVE_MODE` from the `from compressor import ...` list. Keep the rest of the list unchanged.
- `proxy/server.py` ~line 934: `"summarizer_mode": "native" if NATIVE_MODE else f"flattened/{SUMMARIZER_FORMAT}"` becomes `... if compressor.native_mode() else ...`. `server.py` already imports the `compressor` module.
- `proxy/server.py` ~line 1245: the startup log line uses `NATIVE_MODE` in an f-string; change it to `compressor.native_mode()`.

- [ ] **Step 4: Update the tests that assert on the constant**

`tests/test_native_mode_model.py` reads the constant directly at lines 58, 62, 69, 72, 138, 139, 146, 147 (`mod.NATIVE_MODE`, `mod.SUMMARIZER_URL_SET`, `comp.NATIVE_MODE`, `srv.NATIVE_MODE`) and its line-34 docstring states the value is a module-level constant computed at import time. Those assertions encode the design being replaced.

Rewrite each to call `comp.native_mode()`. The two `srv.NATIVE_MODE` assertions (lines 139, 147) have no replacement — `server` no longer holds the flag — so assert `comp.native_mode()` once instead of twice, and drop the `SUMMARIZER_URL_SET` assertion at line 69 in favour of asserting the function's result under the same environment. Update the line-34 docstring to say the value is computed per call.

Preserve every existing behavioural assertion: the point of these tests is that a pinned model or a custom summarizer URL disables native mode. Only the mechanism being asserted changes, never the expected truth value.

- [ ] **Step 5: Run all three environments**

```bash
python3 -m pytest tests/ -q
env ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ROLLING_CONTEXT_PORT=8787 python3 -m pytest tests/ -q
env ROLLING_CONTEXT_SUMMARIZER_URL=http://127.0.0.1:9999 python3 -m pytest tests/ -q
```
Expected: all three green. The third is this task's acceptance test.

- [ ] **Step 6: Mutation-prove**

Change `native_mode()` to `return True` unconditionally, run the suite, confirm the pinned-model and custom-URL tests in `test_native_mode_model.py` fail by name, restore, prove restore with sha256.

- [ ] **Step 7: Commit**

```bash
git add proxy/compressor.py proxy/server.py tests/test_native_mode_model.py
git commit -m "fix(compressor): resolve native mode per call, not at import

SUMMARIZER_URL_SET and NATIVE_MODE were computed once at module import. The
daemon is long-lived and start-proxy.sh reuses it, so a flag captured at import
outlives the configuration it described -- the same defect class as the frozen
upstream, one module over.

Exporting ROLLING_CONTEXT_SUMMARIZER_URL made six tests fail because the flag
froze before they could set it. The suite is now hermetic against a documented,
supported variable."
```

---

### Task 3: Let the displacement alert fail open

**Ticket:** Gemini-imj

**Files:**
- Modify: `proxy/chain.py` (`_record_alert`, around line 334)
- Test: `tests/test_hook_output.py` (add one test)

**Interfaces:** No signature changes. `_record_alert` keeps returning `bool`.

**Why:** Reproduced: with `$HOME/.claude` writable the hook emits 423 bytes and an alert; at mode 500 it emits 0 bytes and no alert. `_record_alert` calls `save_state`, which raises `OSError` on a read-only directory, and that propagates out of the `should-alert` verb so the hook prints nothing. A read-only `$HOME` is a real configuration (containers, hardened CI images).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_hook_output.py`, matching the file's existing helpers and style:

```python
    def test_an_unwritable_state_dir_does_not_silence_the_alert(self):
        """D8 suppression is ergonomics; silence is the bug this feature removes.

        When the state file cannot be written we lose only the memory of having
        alerted, so the alert repeats next session. Losing the alert itself is
        the failure mode the whole feature exists to prevent.
        """
        self._displace(FOREIGN)
        os.chmod(os.path.join(self.home, ".claude"), 0o500)
        self.addCleanup(os.chmod, os.path.join(self.home, ".claude"), 0o700)
        out = self._run_hook().stdout
        self.assertIn("8787", out)
```

Adapt `self._displace(...)` and `self._run_hook()` to whatever the file's existing helpers are actually named — read the file first and reuse them rather than inventing new ones.

- [ ] **Step 2: Run it to see it fail**

Run: `python3 -m pytest tests/test_hook_output.py -q`
Expected: FAIL — stdout is empty.

- [ ] **Step 3: Fail open in _record_alert only**

In `proxy/chain.py`, `_record_alert` currently ends:

```python
    state.setdefault("alerted", []).append({"project": project, "url": key})
    save_state(state)
    return True
```

Change the middle line to:

```python
    state.setdefault("alerted", []).append({"project": project, "url": key})
    # Fail open: if we cannot remember having alerted, still alert. The cost is
    # a repeat next session; the cost of failing closed is silence, which is the
    # failure this whole feature exists to remove.
    with contextlib.suppress(OSError):
        save_state(state)
    return True
```

`contextlib` is already imported in this module.

Do **not** move the suppression inside `save_state` itself. `do_chain` and `do_unchain` also call it, and a silent failure there would write settings without recording them, leaving `unchain` unable to restore.

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/ -q`
Expected: all green, one more test than Task 2 left.

- [ ] **Step 5: Mutation-prove**

Remove the `with contextlib.suppress(OSError):` wrapper (restoring the bare `save_state(state)`), run the suite, confirm `test_an_unwritable_state_dir_does_not_silence_the_alert` fails, restore, prove restore with sha256.

- [ ] **Step 6: Commit**

```bash
git add proxy/chain.py tests/test_hook_output.py
git commit -m "fix(chain): alert even when the state file cannot be written

_record_alert called save_state, which raises OSError on a read-only
\$HOME/.claude and propagated out of the should-alert verb, so the hook printed
nothing at all. A read-only home is a real configuration.

Failing open costs a repeated alert next session. Failing closed costs the
alert, which is the silence this feature exists to remove. The suppression is
deliberately in _record_alert and not in save_state: do_chain and do_unchain
also call it, and a silent failure there would write settings without recording
them."
```

---

### Task 4: Pin the two unconstrained behaviours in server.py

**Ticket:** Gemini-uq5

**Files:**
- Modify: `tests/test_health_chain_fields.py` (add one test)
- Modify: `tests/test_server_upstream.py` (add one test)

**Interfaces:** No source changes. This task adds assertions only.

**Why:** Both mutations leave the suite fully green on master, so nothing pins either behaviour:

```
chained = up.host != "api.anthropic.com"   ->  ==            : 233 passed
if candidate and not chain.is_self(candidate):  ->  if candidate and True:  : 233 passed
```

The code is correct in both places. The gap is that existing tests only ever observe one side of each.

- [ ] **Step 1: Assert the chained polarity in both directions**

Add to `tests/test_health_chain_fields.py`, reusing its existing helpers:

```python
    def test_chained_is_false_for_the_default_api_and_true_for_a_local_upstream(self):
        """Both directions in one test. Asserting only one lets the operator flip."""
        payload = self._health_with_upstream(None)
        self.assertFalse(payload["chained"])
        payload = self._health_with_upstream("http://127.0.0.1:8787")
        self.assertTrue(payload["chained"])
```

Read the file first and reuse its real helper for producing a `/health` payload with a given upstream; do not invent `_health_with_upstream` if something equivalent already exists.

- [ ] **Step 2: Assert the foreign side of the self-guard**

Add to `tests/test_server_upstream.py`:

```python
    def test_a_foreign_loopback_base_url_is_used_not_discarded_as_self(self):
        """The self-guard's untested direction.

        Existing tests cover a self-pointer falling through to the default,
        which an always-true guard also satisfies. What nothing covers is a
        loopback upstream that is NOT us actually being used.
        """
        self._write_user_settings({"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"})
        up = server.current_upstream()
        self.assertEqual(up.port, 8787)
        self.assertEqual(up.host, "127.0.0.1")
```

Again, reuse the file's existing settings-writing and cache-clearing helpers.

- [ ] **Step 3: Run the tests**

Run: `python3 -m pytest tests/ -q`
Expected: all green, two more tests than Task 3 left.

- [ ] **Step 4: Mutation-prove both**

Run each of these, confirming the named test fails, then restoring and proving with sha256:
- `chained = up.host != "api.anthropic.com"` → `==` must fail `test_chained_is_false_for_the_default_api_and_true_for_a_local_upstream`.
- `if candidate and not chain.is_self(candidate):` → `if candidate and True:` must fail `test_a_foreign_loopback_base_url_is_used_not_discarded_as_self`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_health_chain_fields.py tests/test_server_upstream.py
git commit -m "test(server): pin the chained polarity and the self-guard's foreign side

Both behaviours survived mutation with the whole suite green: flipping the
chained comparison, and forcing the ANTHROPIC_BASE_URL self-guard always-true.
The code was right in both places; nothing observed the other direction."
```

---

### Task 5: Cover the two is_self parse-failure arms

**Ticket:** Gemini-3qx

**Files:**
- Modify: `tests/test_is_self.py` (add one test)

**Interfaces:** No source changes.

**Why, and a correction to the ticket:** the ticket claims a CRLF in a settings URL crashes this. That is **false** on this Python:

```
urlparse('http://127.0.0.1:5588\r').port -> 5588      # \r and \n are stripped
```

The two `except ValueError: return False` arms are still reachable, by other inputs:
- the arm around `urlparse()` — `urlparse('http://[::1')` raises `Invalid IPv6 URL` at parse time
- the arm around `.port` — `':abc'`, `':99999'` (out of range), `': 5588'`

`return False` is the right answer at every one of the eight call sites (`chain.py:143,417,509,513,583,594`; `server.py:170,181,185`). The sharpest is the `server.py:170` self-loop guard, where failing open looks risky — but an unparseable URL cannot form a forwarding loop because it cannot form a connection at all. That argument depends on Task 1 having landed, which is why this task is sequenced last.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_is_self.py`:

```python
    def test_unparseable_urls_are_not_us(self):
        """Both parse-failure arms. \\r and \\n do NOT reach them -- Python strips
        those -- so the arms need inputs that genuinely fail to parse."""
        for value in ("http://[::1",          # urlparse() raises: Invalid IPv6 URL
                      "http://127.0.0.1:abc",  # .port raises: not an integer
                      "http://127.0.0.1:99999",  # .port raises: out of range
                      "http://127.0.0.1: 5588"):  # .port raises: not an integer
            with self.subTest(value=value):
                self.assertFalse(chain.is_self(value))
```

- [ ] **Step 2: Run it to see it pass, then prove it constrains**

This test passes immediately — the arms already return `False`. That means Step 2 is the mutation, not the run: change each arm's `return False` to `return True` in turn, confirm this test fails each time, restore, prove restore with sha256. A test that cannot fail is not evidence.

- [ ] **Step 3: Run the tests**

Run: `python3 -m pytest tests/ -q`
Expected: all green.

- [ ] **Step 4: Correct the ticket**

Leave a `bd comment` on `Gemini-3qx` recording that the CRLF trigger in its description is wrong on this Python, and that the real triggers are a malformed IPv6 literal and a non-numeric, out-of-range, or space-padded port.

- [ ] **Step 5: Commit**

```bash
git add tests/test_is_self.py
git commit -m "test(chain): cover both is_self parse-failure arms

Neither arm was exercised. The originally-reported trigger, a CRLF in the URL,
does not reach them -- Python strips \\r and \\n -- so the test uses inputs that
genuinely fail to parse: a malformed IPv6 literal, and ports that are
non-numeric, out of range, or space-padded."
```
