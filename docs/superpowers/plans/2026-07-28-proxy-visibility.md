# Proxy Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** When `headroom wrap claude` (or any foreign local proxy) displaces rolling-context, tell the
user, and give them one command that puts rolling-context back in the request path without restarting
Claude Code.

**Architecture:** One new module `proxy/chain.py` owns every chain decision, verb and write. The three
root bugs are fixed where they live: the loopback predicate (`start-proxy.sh:63`), the hook reading the
wrong settings file, and the upstream frozen at import (`server.py:100`). State is two named fields —
the key someone else owns, recorded so it can be given back, and our own key, which needs no restore.

**Tech Stack:** Python 3 stdlib only — `json`, `os`, `fcntl`/`msvcrt`, `urllib.parse`, `unittest`. Bash
for the hook and installer.

**Spec:** `docs/superpowers/specs/2026-07-28-proxy-visibility-design.md` (design-review-gate approved,
state model narrowed after a plan-gate scope review).

**This plan is end to end.** It fixes all three root bugs, ships the alert, all three verbs, the
slash commands the alert names, the uninstall ordering that `chain`'s project-scope write makes
mandatory, and the docs and version bump. PowerShell parity (D3) is the only deferred work, and it is
deferred because `pwsh` is absent on this machine, not because it is optional.

## Global Constraints

- Pure stdlib. No new dependencies. `proxy/server.py` and `proxy/compressor.py` are stdlib-only today.
- Tests run via `python3 -m unittest discover -s tests` from the repo root.
- Test files live in `tests/`, named exactly as §10 of the spec names them. Every file §10 names is
  built by some task in this plan — §10 is the checklist, and nothing on it is silently skipped.
  `test_effective_value.py` and `test_state_version.py` are the two exceptions and both are
  deliberate: the first is built here under that exact name, the second was deleted from §10 when the
  state-file version refusal was cut.
- Never hardcode `5588`. The port comes from `ROLLING_CONTEXT_PORT` or defaults to `5588`, matching
  `proxy/server.py:47`.
- `fcntl` is POSIX-only. Any import of it sits behind a platform check so `chain.py` imports cleanly on
  Windows — an unconditional import breaks every `.ps1` caller.
- Unparseable JSON is refused and reported, never overwritten — the state file and every settings file.
  Review round 4 found a `{}` fallback that destroyed a settings file.
- The state file is written mode `0600`.
- Settings files are read, mutated in memory, written back whole. Never regenerated.
- Project paths are escaped before they reach any message, log line, or the state file.
- Exit codes: `0` success or no-op, `2` refused with a named reason, `1` internal error.
- **We never displace a foreign value unless the user asks.** No automatic chaining, at session start
  or install time.
- Conventional Commits. No AI attribution, no emoji, no `Co-Authored-By`.

---

### Task 1: Measure tier-1 vs tier-2 precedence (§12 open item)

The spec asserts that `ROLLING_CONTEXT_UPSTREAM` in the process environment beats the same key in
`~/.claude/settings.json`, and the `upstream-pinned-by-env` guard exists only because of that. Nothing
measured it. Fact 3 measured a different key from different sources. If this measurement contradicts the
assumption, §7's tier order and that guard's message both change — so it runs before any of them is built.

**Files:**
- Create: `tests/spikes/precedence_probe.py` (with `tests/spikes/__init__.py` absent on purpose — it is a script, not a test module, so `unittest discover` ignores it)
- Modify: `docs/superpowers/specs/2026-07-28-proxy-visibility-design.md` (§2 gains a measured fact, §12
  loses the open item)

**Interfaces:**
- Produces: a recorded fact in §2 stating which source wins, which Phase 2's `upstream-pinned-by-env`
  guard depends on.

- [ ] **Step 1: Write the probe**

  Two local listeners; the settings file names one, the environment names the other. No API contact —
  both listeners answer locally. This mirrors the Fact 1/Fact 3 spikes already in the scratchpad.

```python
"""Does ROLLING_CONTEXT_UPSTREAM in the environment beat the same key in settings.json?

The spec's tier order (section 7) and the upstream-pinned-by-env guard (section 6) both assume it does.
Nothing measured it. This probe does.

Method: run the proxy with ROLLING_CONTEXT_UPSTREAM set in the environment to listener A, and the same
key set in a fake HOME's settings.json to listener B. Send one request through the proxy. Whichever
listener receives it is the winner.

No API contact: both listeners answer locally.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import http.server
import urllib.request

A, B, PROXY = 5941, 5942, 5943
hits = {A: 0, B: 0}


def make(port):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            hits[port] += 1
            body = b'{"type":"message","content":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    return H


def main():
    home = tempfile.mkdtemp(prefix="precedence-")
    os.makedirs(os.path.join(home, ".claude"))
    with open(os.path.join(home, ".claude", "settings.json"), "w") as f:
        json.dump({"env": {"ROLLING_CONTEXT_UPSTREAM": f"http://127.0.0.1:{B}"}}, f)

    servers = []
    for port in (A, B):
        s = http.server.HTTPServer(("127.0.0.1", port), make(port))
        threading.Thread(target=s.serve_forever, daemon=True).start()
        servers.append(s)

    env = dict(os.environ)
    env["HOME"] = home
    env["ROLLING_CONTEXT_PORT"] = str(PROXY)
    env["ROLLING_CONTEXT_UPSTREAM"] = f"http://127.0.0.1:{A}"
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    proc = subprocess.Popen([sys.executable, "server.py"],
                            cwd=os.path.join(repo, "proxy"), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2)
        req = urllib.request.Request(
            f"http://127.0.0.1:{PROXY}/v1/messages",
            data=json.dumps({"model": "claude-opus-5", "messages": [],
                             "max_tokens": 1}).encode(),
            headers={"Content-Type": "application/json", "x-api-key": "probe"})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            print("request error (may still be conclusive):", e)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        for s in servers:
            s.shutdown()
            s.server_close()
        shutil.rmtree(home, ignore_errors=True)

    print(json.dumps({"env_listener_A": hits[A], "settings_listener_B": hits[B]}))
    if hits[A] and not hits[B]:
        print("VERDICT: environment beats settings -- tier order as assumed")
    elif hits[B] and not hits[A]:
        print("VERDICT: settings beats environment -- SPEC IS WRONG, tier order must change")
    else:
        print("VERDICT: INCONCLUSIVE")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it and record what it says**

  Run: `python3 tests/spikes/precedence_probe.py`
  Expected: one of the three verdicts, printed. Do not proceed on an INCONCLUSIVE result — fix the probe
  until it decides.

- [ ] **Step 3: Record the measurement in the spec**

  In §2 ("Measured facts"), add a fact in the same shape as the existing ones, naming the probe file, the
  observed counts, and the verdict. In §12, replace the open item with a line pointing at that fact.

  If the verdict is "settings beats environment": stop and escalate. §7's tier order and §6's
  `upstream-pinned-by-env` guard both need redesign, which is a spec change, not a plan step.

- [ ] **Step 4: Commit**

```bash
git add tests/spikes/precedence_probe.py docs/superpowers/specs/2026-07-28-proxy-visibility-design.md
git commit -m "test(spike): measure ROLLING_CONTEXT_UPSTREAM precedence, close the spec's open item"
```

---

### Task 2: `is-self` predicate and CLI entry point

The single predicate that replaces seven call sites with four different meanings (§6, §9). Phase 4
migrates those sites; this task builds what they will call.

**Files:**
- Create: `proxy/chain.py`
- Create: `proxy/__init__.py` (empty — makes `from proxy import chain` importable from the repo root)
- Test: `tests/test_is_self.py`

**Interfaces:**
- Produces:
  - `our_bind() -> tuple[str, int]` — `("127.0.0.1", LISTEN_PORT)`, port from `ROLLING_CONTEXT_PORT`
    or `5588`.
  - `host_matches(a: str, b: str) -> bool` — loopback spellings equivalent.
  - `is_self(url: str) -> bool` — the §6 contract.
  - CLI: `python3 chain.py is-self <url>` exits `0` when true, `1` when false.

- [ ] **Step 1: Write failing tests**

```python
"""is-self: the single predicate the seven call sites collapse into (spec section 6, section 9).

Run: python3 -m unittest discover -s tests
"""
import os
import unittest
from unittest import mock

from proxy import chain


class IsSelfTest(unittest.TestCase):
    def setUp(self):
        # Hermetic: this machine may export ROLLING_CONTEXT_PORT. Without this the suite
        # passes only when that value happens to equal the default it asserts against.
        patch = mock.patch.dict(os.environ, {}, clear=False)
        patch.start()
        os.environ.pop("ROLLING_CONTEXT_PORT", None)
        self.addCleanup(patch.stop)

    def test_our_own_url_is_self(self):
        self.assertTrue(chain.is_self("http://127.0.0.1:5588"))

    def test_loopback_spellings_are_equivalent(self):
        for host in ("127.0.0.1", "localhost", "[::1]"):
            with self.subTest(host=host):
                self.assertTrue(chain.is_self(f"http://{host}:5588"))

    def test_headroom_on_8787_is_not_self(self):
        # The original defect: any loopback address was treated as us.
        self.assertFalse(chain.is_self("http://127.0.0.1:8787"))

    def test_same_port_different_host_is_not_self(self):
        # is-self classifies; the chain guard decides whether to refuse (section 6).
        self.assertFalse(chain.is_self("http://192.168.1.10:5588"))

    def test_non_default_port_still_self_detects(self):
        with mock.patch.dict(os.environ, {"ROLLING_CONTEXT_PORT": "6001"}):
            self.assertTrue(chain.is_self("http://127.0.0.1:6001"))
            self.assertFalse(chain.is_self("http://127.0.0.1:5588"))

    def test_scheme_default_port_applies_when_absent(self):
        with mock.patch.dict(os.environ, {"ROLLING_CONTEXT_PORT": "80"}):
            self.assertTrue(chain.is_self("http://127.0.0.1"))

    def test_non_http_scheme_is_not_self(self):
        self.assertFalse(chain.is_self("ftp://127.0.0.1:5588"))

    def test_garbage_is_not_self_and_does_not_raise(self):
        for bad in ("", "not a url", "http://", ":::"):
            with self.subTest(bad=bad):
                self.assertFalse(chain.is_self(bad))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and verify it fails**

  Run: `python3 -m unittest tests.test_is_self -v`
  Expected: FAIL — `ModuleNotFoundError: No module named 'proxy.chain'`

- [ ] **Step 3: Write the minimal implementation**

  Note `proxy/` needs `__init__.py` for `from proxy import chain` to work; create it empty.

```python
"""chain.py — every chain decision, verb, and write primitive lives here.

Imported as a library by proxy/server.py and the hooks; run as a CLI by the shell and PowerShell
wiring. Pure stdlib: it must import cleanly on Windows, where fcntl does not exist.

Spec: docs/superpowers/specs/2026-07-28-proxy-visibility-design.md
"""
import os
import sys
from urllib.parse import urlparse

DEFAULT_PORT = 5588
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
_SCHEME_PORTS = {"http": 80, "https": 443}


def our_bind():
    """The address this daemon actually binds, never a hardcoded constant.

    Mirrors proxy/server.py:47 so a non-default ROLLING_CONTEXT_PORT self-detects correctly.
    """
    return "127.0.0.1", int(os.environ.get("ROLLING_CONTEXT_PORT") or DEFAULT_PORT)


def host_matches(a, b):
    """Loopback spellings are one host. Everything else compares literally."""
    if a is None or b is None:
        return False
    a = a.strip("[]").lower()
    b = b.strip("[]").lower()
    if a in _LOOPBACK and b in _LOOPBACK:
        return True
    return a == b


def is_self(url):
    """True when url names this daemon (spec section 6).

    Deliberately false for a foreign proxy on our port at another host: that is not us. The chain
    guard refuses it separately; classification and policy are different questions.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in _SCHEME_PORTS:
        return False
    try:
        port = parsed.port or _SCHEME_PORTS[parsed.scheme]
    except ValueError:
        return False
    host, our_port = our_bind()
    return host_matches(parsed.hostname, host) and port == our_port


def main(argv):
    if len(argv) >= 2 and argv[0] == "is-self":
        return 0 if is_self(argv[1]) else 1
    sys.stderr.write("usage: chain.py is-self <url>\n")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run and verify it passes**

  Run: `python3 -m unittest tests.test_is_self -v`
  Expected: PASS, 8 tests.

  Then check the CLI both ways:
  Run: `cd proxy && python3 chain.py is-self http://127.0.0.1:5588; echo "exit=$?"`
  Expected: `exit=0`
  Run: `cd proxy && python3 chain.py is-self http://127.0.0.1:8787; echo "exit=$?"`
  Expected: `exit=1`

- [ ] **Step 5: Commit**

```bash
git add proxy/chain.py proxy/__init__.py tests/test_is_self.py
git commit -m "feat(chain): add is-self predicate, one contract for seven call sites"
```

---

### Task 3: Settings resolution — the effective value and the file that supplies it

`chain` and `status` both need to know which file wins, not only what the value is (§6). The Fact 3
order is measured and recorded in §2 of the spec.

**Files:**
- Modify: `proxy/chain.py`
- Test: `tests/test_effective_value.py`

**Interfaces:**
- Consumes: nothing from Task 2 beyond the module.
- Produces:
  - `managed_settings_path()` — the administrator-policy file, highest precedence of all.
  - `settings_scopes(project_root)` — the ordered scope list, highest precedence first; the process
    environment is checked last, as the **weakest** scope per Fact 3.
  - `read_settings(path) -> dict` — parsed `env` block; raises `UnparseableSettings` on bad JSON.
  - `effective(key, project_root) -> tuple[str | None, str | None]` — `(value, source_path)`;
    `(None, None)` when unset everywhere. `source_path` is `"<environment>"` when the process
    environment supplies it.
  - `UnparseableSettings` — exception carrying `.path`.

- [ ] **Step 1: Write failing tests**

```python
"""Which settings file supplies the effective value, in the Fact 3 order (spec section 2, section 6).

Run: python3 -m unittest discover -s tests
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from proxy import chain

KEY = "ANTHROPIC_BASE_URL"


class EffectiveValueTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="effective-")
        self.project = tempfile.mkdtemp(prefix="effective-proj-")
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        os.makedirs(os.path.join(self.project, ".claude"), exist_ok=True)
        self.env_patch = mock.patch.dict(os.environ, {"HOME": self.home}, clear=False)
        self.env_patch.start()
        # Hermetic: this machine may export ANTHROPIC_BASE_URL (headroom does).
        # patch.dict restores the whole mapping on stop, so these pops are undone with it.
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        os.environ.pop("ROLLING_CONTEXT_UPSTREAM", None)
        os.environ.pop("ROLLING_CONTEXT_PORT", None)
        self.addCleanup(self.env_patch.stop)

    def _write(self, path, value):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"env": {KEY: value}}, f)

    def test_unset_everywhere_reports_nothing(self):
        self.assertEqual(chain.effective(KEY, self.project), (None, None))

    def test_user_settings_supply_the_value(self):
        path = os.path.join(self.home, ".claude", "settings.json")
        self._write(path, "http://127.0.0.1:1111")
        value, source = chain.effective(KEY, self.project)
        self.assertEqual(value, "http://127.0.0.1:1111")
        self.assertEqual(source, path)

    def test_project_local_beats_user_settings(self):
        self._write(os.path.join(self.home, ".claude", "settings.json"), "http://127.0.0.1:1111")
        local = os.path.join(self.project, ".claude", "settings.local.json")
        self._write(local, "http://127.0.0.1:2222")
        value, source = chain.effective(KEY, self.project)
        self.assertEqual(value, "http://127.0.0.1:2222")
        self.assertEqual(source, local)

    def test_source_is_reported_not_only_the_value(self):
        local = os.path.join(self.project, ".claude", "settings.local.json")
        self._write(local, "http://127.0.0.1:2222")
        _, source = chain.effective(KEY, self.project)
        self.assertTrue(source.endswith("settings.local.json"))

    def test_unparseable_settings_raise_rather_than_defaulting(self):
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertRaises(chain.UnparseableSettings) as ctx:
            chain.effective(KEY, self.project)
        self.assertEqual(ctx.exception.path, path)

    def test_environment_supplies_the_value_when_no_file_does(self):
        # The weakest scope still wins when nothing else sets the key. Without this test
        # the entire env-fallback branch is never executed by the suite.
        with mock.patch.dict(os.environ, {KEY: "http://127.0.0.1:3333"}):
            value, source = chain.effective(KEY, self.project)
        self.assertEqual(value, "http://127.0.0.1:3333")
        self.assertEqual(source, "<environment>")

    def test_a_file_beats_the_environment(self):
        # The regression test for precedence inversion. An earlier draft of this design had
        # the environment checked first; this is what would have caught it.
        local = os.path.join(self.project, ".claude", "settings.local.json")
        self._write(local, "http://127.0.0.1:2222")
        with mock.patch.dict(os.environ, {KEY: "http://127.0.0.1:3333"}):
            value, source = chain.effective(KEY, self.project)
        self.assertEqual(value, "http://127.0.0.1:2222")
        self.assertEqual(source, local)

    def test_missing_env_block_is_not_an_error(self):
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"permissions": {}}, f)
        self.assertEqual(chain.effective(KEY, self.project), (None, None))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and verify it fails**

  Run: `python3 -m unittest tests.test_effective_value -v`
  Expected: FAIL — `AttributeError: module 'proxy.chain' has no attribute 'effective'`

- [ ] **Step 3: Write the minimal implementation**

  Append to `proxy/chain.py`:

```python
import json


class UnparseableSettings(Exception):
    """A settings file is not valid JSON. We refuse to touch it rather than overwrite it."""

    def __init__(self, path):
        super().__init__(f"{path} is not valid JSON")
        self.path = path


def user_settings_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def managed_settings_path():
    """Administrator policy file. Highest precedence, and unwinnable by a write.

    Paths per the Claude Code settings docs. This covers the file delivery channel only --
    managed settings can also arrive by macOS plist, Windows registry (HKLM/HKCU), or
    managed-settings.d/ drop-ins, none of which a stdlib file read can see. The guard below
    is therefore a courtesy that produces a precise message when it fires; the guarantee that
    we never leave a write in place that cannot win comes from chain's effective-value
    read-back, which is channel-agnostic.
    """
    if sys.platform == "darwin":
        return "/Library/Application Support/ClaudeCode/managed-settings.json"
    if os.name == "nt":
        return r"C:\ProgramData\ClaudeCode\managed-settings.json"
    return "/etc/claude-code/managed-settings.json"


def settings_scopes(project_root):
    """Files that can supply a value, highest precedence first (spec section 2, Fact 3)."""
    scopes = [managed_settings_path()]
    if project_root:
        scopes.append(os.path.join(project_root, ".claude", "settings.local.json"))
        scopes.append(os.path.join(project_root, ".claude", "settings.json"))
    scopes.append(user_settings_path())
    return scopes


def read_settings(path):
    """Parsed contents, or {} when absent. Raises UnparseableSettings on invalid JSON."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise UnparseableSettings(path)


def effective(key, project_root):
    """(value, source_path) for key, or (None, None).

    Fact 3, measured: managed > project-local > project-shared > user > process-env. The process
    environment is the WEAKEST scope, not the strongest -- a foreign proxy that only sets a child
    environment cannot displace a settings-file value, which is exactly why displacement always
    originates in a file. Checking the environment first would build the whole displacement
    decision on a value Claude Code itself would not honour.
    """
    for path in settings_scopes(project_root):
        env_block = read_settings(path).get("env") or {}
        value = env_block.get(key)
        if value:
            return value, path
    from_env = os.environ.get(key)
    if from_env:
        return from_env, "<environment>"
    return None, None
```

- [ ] **Step 4: Run and verify it passes**

  Run: `python3 -m unittest tests.test_effective_value -v`
  Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add proxy/chain.py tests/test_effective_value.py
git commit -m "feat(chain): resolve the effective value and report which file supplies it"
```

---

### Task 4: Escape project paths before they are shown

A project directory name is chosen by whoever created it and arrives via `git clone`, and per D6 these
verbs also ship as slash commands — so their output is read by the model, not only by a person at a
terminal. One function, applied wherever a path reaches a message, a log line, or the state file.

**Files:**
- Modify: `proxy/chain.py`
- Test: `tests/test_path_sanitizing.py`

**Interfaces:**
- Produces: `display(text) -> str` — escapes control and non-printable bytes.

- [ ] **Step 1: Write the failing test**

```python
"""Project paths are escaped before display (spec section 5).

Structural guarantee only: control bytes cannot reach a terminal, a log line, or the state file.
NOT an anti-prompt-injection measure -- printable text has nothing to escape, and a test records
that limit explicitly so nobody mistakes the one for the other.

Run: python3 -m unittest discover -s tests
"""
import unittest

from proxy import chain


class DisplayTest(unittest.TestCase):
    def test_escape_sequences_cannot_reach_the_terminal(self):
        self.assertNotIn("\x1b", chain.display("/tmp/\x1b[31mred\x1b[0m"))

    def test_newlines_cannot_break_a_log_line(self):
        self.assertNotIn("\n", chain.display("/tmp/a\nb"))

    def test_printable_text_is_left_alone(self):
        # The honest limit, recorded on purpose: nothing here to escape.
        plain = "/tmp/ignore previous instructions"
        self.assertEqual(chain.display(plain), plain)

    def test_none_is_empty(self):
        self.assertEqual(chain.display(None), "")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and verify it fails**

  Run: `python3 -m unittest tests.test_path_sanitizing -v`
  Expected: FAIL — `AttributeError: module 'proxy.chain' has no attribute 'display'`

- [ ] **Step 3: Write the minimal implementation**

  Append to `proxy/chain.py`:

```python
def display(text):
    """Escape control and non-printable bytes for any message, log line, or state-file value.

    Structural safety only: no terminal escapes, no injected newlines, no corrupted JSON. A name
    written in plain printable text has nothing to escape and passes through unchanged -- the same
    residue section 7 accepts for URL path components.
    """
    if text is None:
        return ""
    return "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in str(text))
```

- [ ] **Step 4: Run and verify it passes**

  Run: `python3 -m unittest tests.test_path_sanitizing -v`
  Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add proxy/chain.py tests/test_path_sanitizing.py
git commit -m "feat(chain): escape control bytes in project paths before display"
```

---

### Task 5: State file — two named fields, atomic replace, mode 0600

`abu` records the key someone else owns, so `unchain` can give the value back. `upstream` records our
own key, which has no displaced value because nothing else writes it. See spec section 5.

**Files:**
- Modify: `proxy/chain.py`
- Test: `tests/test_state_io.py`

**Interfaces:**
- Produces:
  - `state_path() -> str`
  - `empty_state() -> dict` — `{"abu": {}, "upstream": None, "alerted": []}`; `abu` is a map
    keyed by project path, `upstream` is a single object with no `displaced` field
  - `load_state() -> dict` — raises `UnparseableSettings` on bad JSON
  - `save_state(state) -> None` — atomic `os.replace`, mode `0600`
  (No lock. Atomic `os.replace` is the whole concurrency story — see §5 Writing rules.)

- [ ] **Step 1: Write the failing test**

```python
"""State file I/O: atomic, locked, 0600, refusing rather than overwriting (spec section 5).

Run: python3 -m unittest discover -s tests
"""
import os
import stat
import tempfile
import unittest
from unittest import mock

from proxy import chain


class StateIOTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="state-")
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        patch = mock.patch.dict(os.environ, {"HOME": self.home}, clear=False)
        patch.start()
        # Hermetic: this machine may export ANTHROPIC_BASE_URL (headroom does).
        # patch.dict restores the whole mapping on stop, so these pops are undone with it.
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        os.environ.pop("ROLLING_CONTEXT_UPSTREAM", None)
        os.environ.pop("ROLLING_CONTEXT_PORT", None)
        self.addCleanup(patch.stop)

    def test_absent_state_reads_as_empty(self):
        self.assertEqual(chain.load_state(), chain.empty_state())

    def test_round_trip_both_fields(self):
        state = chain.empty_state()
        state["abu"] = {"/p": {"path": "/p/.claude/settings.local.json",
                               "wrote": "http://127.0.0.1:5588",
                               "displaced": "http://127.0.0.1:8787"}}
        state["upstream"] = {"wrote": "http://127.0.0.1:8787"}
        chain.save_state(state)
        self.assertEqual(chain.load_state(), state)

    def test_upstream_has_no_displaced_field(self):
        # It is our own key. Recording a value to restore to is what produced the
        # ordering bug an earlier draft had -- the field is deliberately absent.
        state = chain.empty_state()
        state["upstream"] = {"wrote": "http://127.0.0.1:8787"}
        chain.save_state(state)
        self.assertNotIn("displaced", chain.load_state()["upstream"])

    def test_written_mode_is_0600(self):
        chain.save_state(chain.empty_state())
        self.assertEqual(stat.S_IMODE(os.stat(chain.state_path()).st_mode), 0o600)

    def test_rewrite_keeps_mode_0600(self):
        chain.save_state(chain.empty_state())
        chain.save_state(chain.empty_state())
        self.assertEqual(stat.S_IMODE(os.stat(chain.state_path()).st_mode), 0o600)

    def test_rewrite_tightens_a_loose_pre_existing_file(self):
        # The mode test with teeth. tempfile.mkstemp already opens at 0600, so dropping the
        # chmod leaves the other two mode tests green -- they assert a true property without
        # isolating what produces it. This one fails against a plain in-place open(path, "w"),
        # which would silently inherit 0644 from whatever put the file there.
        with open(chain.state_path(), "w", encoding="utf-8") as f:
            json.dump(chain.empty_state(), f)
        os.chmod(chain.state_path(), 0o644)
        chain.save_state(chain.empty_state())
        self.assertEqual(stat.S_IMODE(os.stat(chain.state_path()).st_mode), 0o600)

    def test_no_temp_file_is_left_behind(self):
        chain.save_state(chain.empty_state())
        leftovers = [n for n in os.listdir(os.path.join(self.home, ".claude"))
                     if n.startswith(".rolling-context-state-")]
        self.assertEqual(leftovers, [])

    def test_unparseable_state_refuses_rather_than_overwriting(self):
        with open(chain.state_path(), "w", encoding="utf-8") as f:
            f.write("{ broken")
        with self.assertRaises(chain.UnparseableSettings):
            chain.load_state()
        with open(chain.state_path(), encoding="utf-8") as f:
            self.assertEqual(f.read(), "{ broken")

if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and verify it fails**

  Run: `python3 -m unittest tests.test_state_io -v`
  Expected: FAIL — `AttributeError: module 'proxy.chain' has no attribute 'load_state'`

- [ ] **Step 3: Write the minimal implementation**

  Append to `proxy/chain.py`. No `fcntl` anywhere, so nothing here can break the `.ps1` callers on
  Windows — `os.replace` is atomic on both platforms.

```python
import contextlib
import tempfile


def state_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "rolling-context-proxy.json")


def empty_state():
    """abu: the key someone else owns, so it carries what we displaced.
    upstream: our own key, so it carries only who is still chained through it."""
    return {"abu": {}, "upstream": None, "alerted": []}


def load_state():
    path = state_path()
    if not os.path.exists(path):
        return empty_state()
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise UnparseableSettings(path)


def save_state(state):
    """Atomic replace at mode 0600 -- it names project paths and local proxy topology."""
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".rolling-context-state-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
```

- [ ] **Step 4: Run and verify it passes**

  Run: `python3 -m unittest tests.test_state_io -v`
  Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add proxy/chain.py tests/test_state_io.py
git commit -m "feat(chain): state file with exclusive lock, atomic replace, mode 0600"
```

---

### Task 6: The three verbs — `chain`, `unchain`, `status`

The user-visible fix. `chain` is the single command from R2; `unchain` gives back what it took;
`status` is what makes silence recoverable. Spec section 6.

**Files:**
- Modify: `proxy/chain.py`
- Create: `hooks/chain.sh` — the D6 entry point. The dead-upstream error in Task 7 tells the user
  to run `bash <resolved>/hooks/chain.sh unchain`, so this file must exist or that instruction
  fails with "no such file or directory" at the exact moment the upstream is dead.
- Test: `tests/test_chain_verb.py`, `tests/test_unchain_verb.py`, `tests/test_status_verb.py`,
  `tests/test_entry_point.py`

**Interfaces:**
- Consumes: `is_self`, `effective`, `display`, `load_state`, `save_state`,
  `UnparseableSettings` (Tasks 2–5). There is no lock in this design.
- Produces:
  - `project_root(start) -> str | None` — nearest ancestor containing `.claude`, stopping strictly
    before `$HOME`.
  - `Refusal` — exception carrying `.reason` and `.message`.
  - `do_chain(project, assume_yes=False) -> int`, `do_unchain(project, all_=False) -> int`,
    `do_status(project) -> int` — exit codes per the Global Constraints.
  - CLI verbs: `chain [--yes]`, `unchain [--all]`, `status`, and `effective-abu` — which prints the
    winning `ANTHROPIC_BASE_URL` and nothing else, because Task 8's hook consumes it as
    `$(chain.py effective-abu)`. Without this verb the hook's detection is dead and the whole
    feature silently no-ops.

- [ ] **Step 1: Write the failing tests**

```python
"""chain: guards refuse without writing; apply writes upstream first, then base URL.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from proxy import chain

FOREIGN = "http://127.0.0.1:8787"


class ChainVerbTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="chain-home-")
        self.project = tempfile.mkdtemp(prefix="chain-proj-")
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        os.makedirs(os.path.join(self.project, ".claude"), exist_ok=True)
        patch = mock.patch.dict(os.environ, {"HOME": self.home}, clear=False)
        patch.start()
        # Hermetic: this machine may export ANTHROPIC_BASE_URL (headroom does).
        # patch.dict restores the whole mapping on stop, so these pops are undone with it.
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        os.environ.pop("ROLLING_CONTEXT_UPSTREAM", None)
        os.environ.pop("ROLLING_CONTEXT_PORT", None)
        self.addCleanup(patch.stop)
        self.local = os.path.join(self.project, ".claude", "settings.local.json")

    def _displace(self, url=FOREIGN):
        with open(self.local, "w", encoding="utf-8") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": url}}, f)

    def _user_env(self):
        path = os.path.join(self.home, ".claude", "settings.json")
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("env", {})

    def _local_env(self):
        with open(self.local, encoding="utf-8") as f:
            return json.load(f).get("env", {})

    def test_chain_writes_both_keys_upstream_first(self):
        self._displace()
        self.assertEqual(chain.do_chain(self.project, assume_yes=True), 0)
        self.assertEqual(self._user_env()["ROLLING_CONTEXT_UPSTREAM"], FOREIGN)
        self.assertTrue(chain.is_self(self._local_env()["ANTHROPIC_BASE_URL"]))

    def test_chain_records_what_it_displaced(self):
        self._displace()
        chain.do_chain(self.project, assume_yes=True)
        state = chain.load_state()
        self.assertEqual(state["abu"][self.project]["displaced"], FOREIGN)
        self.assertEqual(state["abu"][self.project]["path"], self.local)
        self.assertEqual(state["upstream"]["wrote"], FOREIGN)
        self.assertNotIn("displaced", state["upstream"])

    def test_nothing_to_chain_is_an_exit_zero_noop(self):
        self.assertEqual(chain.do_chain(self.project, assume_yes=True), 0)
        self.assertEqual(chain.load_state(), chain.empty_state())

    def test_already_ours_is_an_exit_zero_noop(self):
        self._displace("http://127.0.0.1:5588")
        self.assertEqual(chain.do_chain(self.project, assume_yes=True), 0)

    def test_non_loopback_is_refused_and_writes_nothing(self):
        self._displace("https://proxy.example.com")
        self.assertEqual(chain.do_chain(self.project, assume_yes=True), 2)
        self.assertEqual(chain.load_state(), chain.empty_state())

    def test_declined_confirmation_writes_nothing(self):
        self._displace()
        with mock.patch("builtins.input", return_value="n"):
            self.assertEqual(chain.do_chain(self.project, assume_yes=False), 2)
        self.assertEqual(chain.load_state(), chain.empty_state())
        self.assertEqual(self._local_env()["ANTHROPIC_BASE_URL"], FOREIGN)

    def test_non_interactive_without_yes_refuses_rather_than_hanging(self):
        self._displace()
        with mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(chain.do_chain(self.project, assume_yes=False), 2)

    def test_divergent_chain_is_refused(self):
        self._displace()
        chain.do_chain(self.project, assume_yes=True)
        self._displace("http://127.0.0.1:9999")
        self.assertEqual(chain.do_chain(self.project, assume_yes=True), 2)

    def test_env_pinned_upstream_is_refused(self):
        self._displace()
        with mock.patch.dict(os.environ, {"ROLLING_CONTEXT_UPSTREAM": FOREIGN}):
            self.assertEqual(chain.do_chain(self.project, assume_yes=True), 2)

    def test_effective_abu_prints_the_winning_value_and_nothing_else(self):
        # The hook consumes this as $(chain.py effective-abu). Extra output breaks it.
        self._displace()
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = chain.main(["effective-abu"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), FOREIGN)

    def test_matching_url_second_chain_succeeds_and_appends_a_ref(self):
        # D10: a second project through the same proxy needs the same upstream.
        self._displace()
        chain.do_chain(self.project, assume_yes=True)
        other = tempfile.mkdtemp(prefix="chain-proj2-")
        os.makedirs(os.path.join(other, ".claude"), exist_ok=True)
        with open(os.path.join(other, ".claude", "settings.local.json"), "w") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": FOREIGN}}, f)
        self.assertEqual(chain.do_chain(other, assume_yes=True), 0)
        self.assertEqual(chain.load_state()["upstream"]["wrote"], FOREIGN)

    def test_env_override_without_displacement_is_a_noop_not_a_refusal(self):
        # A user with a legitimate ROLLING_CONTEXT_UPSTREAM and nothing displacing them
        # should be told there is nothing to chain, not told to unset their variable.
        with mock.patch.dict(os.environ, {"ROLLING_CONTEXT_UPSTREAM": FOREIGN}):
            self.assertEqual(chain.do_chain(self.project, assume_yes=True), 0)

    def test_a_higher_scope_that_outranks_our_write_undoes_it(self):
        # A managed policy can arrive by a channel no file check sees. The effective-value
        # read-back is what catches it, so this asserts the undo, not the detection.
        self._displace()
        higher = os.path.join(self.project, ".claude", "settings.local.json")
        real_effective = chain.effective

        def outranked(key, root):
            if key == chain.ABU_KEY:
                return FOREIGN, "/etc/claude-code/managed-settings.json"
            return real_effective(key, root)

        with mock.patch.object(chain, "effective", side_effect=outranked):
            self.assertEqual(chain.do_chain(self.project, assume_yes=True), 1)
        self.assertEqual(chain.load_state(), chain.empty_state())
        with open(higher, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["env"]["ANTHROPIC_BASE_URL"], FOREIGN)

    def test_unparseable_settings_refuses_and_leaves_the_file(self):
        with open(self.local, "w", encoding="utf-8") as f:
            f.write("{ broken")
        self.assertEqual(chain.do_chain(self.project, assume_yes=True), 2)
        with open(self.local, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{ broken")


if __name__ == "__main__":
    unittest.main()
```

```python
"""unchain: give back what we took; our own key is deleted, not restored (spec section 5, 6).

Run: python3 -m unittest discover -s tests
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from proxy import chain

FOREIGN = "http://127.0.0.1:8787"


class UnchainTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="unchain-home-")
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        patch = mock.patch.dict(os.environ, {"HOME": self.home}, clear=False)
        patch.start()
        # Hermetic: this machine may export ANTHROPIC_BASE_URL (headroom does).
        # patch.dict restores the whole mapping on stop, so these pops are undone with it.
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        os.environ.pop("ROLLING_CONTEXT_UPSTREAM", None)
        os.environ.pop("ROLLING_CONTEXT_PORT", None)
        self.addCleanup(patch.stop)

    def _project(self, name):
        root = tempfile.mkdtemp(prefix=f"unchain-{name}-")
        os.makedirs(os.path.join(root, ".claude"), exist_ok=True)
        with open(os.path.join(root, ".claude", "settings.local.json"), "w", encoding="utf-8") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": FOREIGN}}, f)
        return root

    def _user_env(self):
        path = os.path.join(self.home, ".claude", "settings.json")
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("env", {})

    def _local_env(self, root):
        with open(os.path.join(root, ".claude", "settings.local.json"), encoding="utf-8") as f:
            return json.load(f).get("env", {})

    def test_restores_the_displaced_base_url(self):
        a = self._project("a")
        chain.do_chain(a, assume_yes=True)
        self.assertEqual(chain.do_unchain(a), 0)
        self.assertEqual(self._local_env(a)["ANTHROPIC_BASE_URL"], FOREIGN)

    def test_all_deletes_our_own_key(self):
        a = self._project("a")
        chain.do_chain(a, assume_yes=True)
        chain.do_unchain(a, all_=True)
        self.assertNotIn("ROLLING_CONTEXT_UPSTREAM", self._user_env())

    def test_plain_unchain_leaves_our_own_key_set(self):
        # It is inert for this project once ANTHROPIC_BASE_URL is restored, and a second
        # project may still be chained through it. Only --all removes it (D10).
        a, b = self._project("a"), self._project("b")
        chain.do_chain(a, assume_yes=True)
        chain.do_chain(b, assume_yes=True)
        chain.do_unchain(a)
        self.assertEqual(self._user_env()["ROLLING_CONTEXT_UPSTREAM"], FOREIGN)

    def test_a_second_project_still_resolves_after_the_first_unchains(self):
        a, b = self._project("a"), self._project("b")
        chain.do_chain(a, assume_yes=True)
        chain.do_chain(b, assume_yes=True)
        chain.do_unchain(a)
        self.assertTrue(chain.is_self(self._local_env(b)["ANTHROPIC_BASE_URL"]))
        self.assertEqual(self._user_env()["ROLLING_CONTEXT_UPSTREAM"], FOREIGN)

    def test_deleted_base_url_is_skipped_not_resurrected(self):
        # headroom removes this key when it exits (wrap.py:1779-1781).
        a = self._project("a")
        chain.do_chain(a, assume_yes=True)
        with open(os.path.join(a, ".claude", "settings.local.json"), "w", encoding="utf-8") as f:
            json.dump({"env": {}}, f)
        self.assertEqual(chain.do_unchain(a), 0)
        self.assertNotIn("ANTHROPIC_BASE_URL", self._local_env(a))

    def test_all_skips_when_the_key_is_no_longer_ours(self):
        a = self._project("a")
        chain.do_chain(a, assume_yes=True)
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"env": {"ROLLING_CONTEXT_UPSTREAM": "http://127.0.0.1:9999"}}, f)
        self.assertEqual(chain.do_unchain(a, all_=True), 0)
        self.assertEqual(self._user_env()["ROLLING_CONTEXT_UPSTREAM"], "http://127.0.0.1:9999")

    def test_no_project_ancestor_is_an_exit_zero_report(self):
        self.assertEqual(chain.do_unchain(self.home), 0)


if __name__ == "__main__":
    unittest.main()
```

```python
"""status: reports, never writes (spec section 6).

Run: python3 -m unittest discover -s tests
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from proxy import chain

FOREIGN = "http://127.0.0.1:8787"


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="status-home-")
        self.project = tempfile.mkdtemp(prefix="status-proj-")
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        os.makedirs(os.path.join(self.project, ".claude"), exist_ok=True)
        patch = mock.patch.dict(os.environ, {"HOME": self.home}, clear=False)
        patch.start()
        # Hermetic: this machine may export ANTHROPIC_BASE_URL (headroom does).
        # patch.dict restores the whole mapping on stop, so these pops are undone with it.
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        os.environ.pop("ROLLING_CONTEXT_UPSTREAM", None)
        os.environ.pop("ROLLING_CONTEXT_PORT", None)
        self.addCleanup(patch.stop)

    def _run(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = chain.do_status(self.project)
        return code, buf.getvalue()

    def test_reports_the_displacing_proxy_and_its_source_file(self):
        local = os.path.join(self.project, ".claude", "settings.local.json")
        with open(local, "w", encoding="utf-8") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": FOREIGN}}, f)
        _, out = self._run()
        self.assertIn("8787", out)
        self.assertIn(local, out)

    def test_writes_nothing(self):
        local = os.path.join(self.project, ".claude", "settings.local.json")
        with open(local, "w", encoding="utf-8") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": FOREIGN}}, f)
        chain.do_chain(self.project, assume_yes=True)
        before_state = open(chain.state_path(), "rb").read()
        before_local = open(local, "rb").read()
        self._run()
        self.assertEqual(open(chain.state_path(), "rb").read(), before_state)
        self.assertEqual(open(local, "rb").read(), before_local)

    def test_reports_the_recorded_upstream(self):
        state = chain.empty_state()
        state["upstream"] = {"wrote": FOREIGN}
        chain.save_state(state)
        _, out = self._run()
        self.assertIn("8787", out)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and verify all three fail**

  Run: `python3 -m unittest tests.test_chain_verb tests.test_unchain_verb tests.test_status_verb -v`
  Expected: FAIL — `AttributeError: module 'proxy.chain' has no attribute 'do_chain'`

- [ ] **Step 3: Write the minimal implementation**

  Append to `proxy/chain.py`:

```python
import sys

USER_KEY = "ROLLING_CONTEXT_UPSTREAM"
ABU_KEY = "ANTHROPIC_BASE_URL"


class Refusal(Exception):
    """A named guard refused. Nothing was written."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason
        self.message = message


def project_root(start):
    """Nearest ancestor containing .claude, stopping strictly before $HOME.

    The exclusion is load-bearing: $HOME/.claude always exists, so a walk that did not stop before
    it would treat the home directory as 'the project' and widen every unchain to --all's scope.
    """
    home = os.path.realpath(os.path.expanduser("~"))
    here = os.path.realpath(start)
    while here != os.path.dirname(here):
        if here == home:
            return None
        if os.path.isdir(os.path.join(here, ".claude")):
            return here
        here = os.path.dirname(here)
    return None


def _write_key(path, key, value):
    """Read, mutate in memory, write back whole. Never regenerate the file."""
    data = read_settings(path)
    env = data.setdefault("env", {})
    if value is None:
        env.pop(key, None)
    else:
        env[key] = value
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".rc-settings-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_key(path, key):
    return (read_settings(path).get("env") or {}).get(key)


def _guards(project, state):
    """Every guard refuses with a named reason and writes nothing.

    Displacement is checked FIRST: a user with a legitimate exported upstream and nothing
    displacing them should hear "nothing to chain", not "unset your variable".
    """
    value, source = effective(ABU_KEY, project)
    if value is None:
        return None, None
    if is_self(value):
        return None, None
    if os.environ.get(USER_KEY):
        raise Refusal("upstream-pinned-by-env",
                      f"{USER_KEY} is set in your shell environment "
                      f"({display(os.environ[USER_KEY])}) — settings can't override that. "
                      f"unset it or edit your shell config instead")
    host = urlparse(value).hostname
    if not host_matches(host, "127.0.0.1"):
        raise Refusal("non-loopback",
                      f"refusing to chain to {display(value)} — not a loopback address. "
                      f"rolling-context only chains to local proxies "
                      f"(127.0.0.1/::1/localhost); chaining elsewhere would forward your "
                      f"API key off-machine")
    if source == managed_settings_path():
        raise Refusal("managed-scope",
                      f"{display(value)} is set by managed-settings.json — an administrator "
                      f"policy, not something rolling-context can override")
    if urlparse(value).port == our_bind()[1]:
        raise Refusal("same-port-different-host",
                      f"{display(value)} uses our own port on a different host — refusing, "
                      f"this looks like a misconfiguration rather than a proxy to chain to")
    recorded = (state.get("upstream") or {}).get("wrote")
    if recorded and recorded != value:
        raise Refusal("divergent-chain",
                      f"already chained to {display(recorded)}; {display(value)} is a different "
                      f"proxy. run 'unchain' first if you want to switch")
    return value, source


def _confirm(url, assume_yes):
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    print(f"chain will make {display(url)} the upstream for EVERY project on this machine,")
    print(f"by writing {USER_KEY} to {user_settings_path()}.")
    return input("continue? [y/N] ").strip().lower() == "y"


def do_chain(project, assume_yes=False):
    try:
        state = load_state()
        try:
            url, source = _guards(project, state)
        except Refusal as r:
            print(f"not chained — {r.message}")
            return 2
        if url is None:
            print("no foreign proxy detected — nothing to chain")
            return 0
        if not _confirm(url, assume_yes):
            print("not chained — confirmation declined. re-run with --yes to skip the prompt")
            return 2

        ours = f"http://127.0.0.1:{our_bind()[1]}"
        # Keyed by project: D10 allows two chained at once, and an unkeyed record would let
        # B's chain overwrite A's, after which A's unchain would restore B's displaced value
        # into B's file -- un-chaining a project the user never named.
        state.setdefault("abu", {})[project] = {"path": source, "wrote": ours, "displaced": url}
        state["upstream"] = {"wrote": url}
        save_state(state)

        # Upstream first: reversing this points Claude Code at us before we know where to
        # forward, and "no upstream recorded" resolves to the default API -- silently
        # un-chaining the user, which D9 forbids.
        _write_key(user_settings_path(), USER_KEY, url)
        _write_key(source, ABU_KEY, ours)

        # Read back the EFFECTIVE value, not the file we just wrote. A managed policy -- which
        # may arrive by plist, registry or drop-in, where no file check can see it -- leaves our
        # write in place while the value that actually applies stays foreign. Resolving again is
        # the only channel-agnostic way to know the write won.
        landed, landed_source = effective(ABU_KEY, project)
        if _read_key(user_settings_path(), USER_KEY) != url or not is_self(landed or ""):
            _write_key(source, ABU_KEY, url)
            _write_key(user_settings_path(), USER_KEY, None)
            save_state(empty_state())
            if landed and not is_self(landed):
                print(f"not chained — {display(landed_source)} still supplies "
                      f"{display(landed)}, which outranks what we wrote. changes undone")
            else:
                print("not chained — read-back failed, changes undone")
            return 1
        print(f"chained: {display(url)} is now upstream; rolling-context is back in the path")
        return 0
    except UnparseableSettings as e:
        print(f"not chained — {display(e.path)} is not valid JSON — refusing to touch it. "
              f"fix the file by hand and retry")
        return 2


def do_unchain(project, all_=False):
    try:
        state = load_state()
        root = project if all_ else project_root(project)
        if root is None and not all_:
            print("nothing project-scoped to unchain here")
            return 0

        # Give back the key someone else owns -- this project's record and no other.
        records = state.get("abu") or {}
        targets = list(records) if all_ else ([root] if root in records else [])
        for key in targets:
            abu = records[key]
            if _read_key(abu["path"], ABU_KEY) == abu["wrote"]:
                _write_key(abu["path"], ABU_KEY, abu["displaced"])
            else:
                print(f"skipped {display(abu['path'])} — {ABU_KEY} is no longer ours")
            del records[key]
        state["abu"] = records

        # Our own key is left set. Restoring ANTHROPIC_BASE_URL above already took this
        # project out of the request path, so the upstream value is inert for it -- and
        # another project may still be chained through it (D10). Only --all removes it,
        # which is what uninstall passes and means "nothing is chained any more".
        upstream = state.get("upstream")
        if upstream and all_:
            if _read_key(user_settings_path(), USER_KEY) == upstream["wrote"]:
                _write_key(user_settings_path(), USER_KEY, None)
            else:
                print(f"skipped {USER_KEY} — it is no longer ours")
            state["upstream"] = None
        save_state(state)
        print("unchained")
        return 0
    except UnparseableSettings as e:
        print(f"not unchained — {display(e.path)} is not valid JSON — refusing to touch it")
        return 2


def do_status(project):
    """Reports. Never writes -- a command run anytime must not mutate state."""
    port = our_bind()[1]
    print(f"rolling-context: port {port}")
    try:
        value, source = effective(ABU_KEY, project)
        state = load_state()
    except UnparseableSettings as e:
        print(f"cannot report — {display(e.path)} is not valid JSON")
        return 2

    if value is None:
        print("in path:  no  -- ANTHROPIC_BASE_URL is unset")
    elif is_self(value):
        print(f"in path:  yes -- from {display(source)}")
    else:
        print(f"in path:  no  -- {display(value)} wins, from {display(source)}")

    upstream = state.get("upstream")
    if upstream:
        print(f"chained:  yes -- upstream {display(upstream['wrote'])}")
    else:
        print("chained:  no")

    if value is not None and not is_self(value):
        print("compaction: OFF this session")
        print("fix: /rolling-context:chain")
    return 0
```

  Extend `main()` to route the new verbs:

```python
def main(argv):
    if not argv:
        sys.stderr.write("usage: chain.py {is-self <url>|chain [--yes]|unchain [--all]|status}\n")
        return 1
    verb, rest = argv[0], argv[1:]
    if verb == "is-self":
        return 0 if (rest and is_self(rest[0])) else 1
    cwd = os.getcwd()
    if verb == "chain":
        root = project_root(cwd)
        if root is None:
            print("no project here — run this inside a project directory")
            return 2
        return do_chain(root, assume_yes="--yes" in rest)
    if verb == "unchain":
        return do_unchain(cwd, all_="--all" in rest)
    if verb == "status":
        return do_status(project_root(cwd) or cwd)
    if verb == "effective-abu":
        # What the hook calls. Prints the winning ANTHROPIC_BASE_URL and nothing else, so
        # `$(chain.py effective-abu)` is directly usable in shell. Empty output means unset.
        try:
            value, _ = effective(ABU_KEY, project_root(cwd) or cwd)
        except UnparseableSettings:
            return 2
        if value:
            print(value)
        return 0
    sys.stderr.write(f"unknown verb: {verb}\n")
    return 1
```

- [ ] **Step 4: Run and verify they pass**

  Run: `python3 -m unittest tests.test_chain_verb tests.test_unchain_verb tests.test_status_verb -v`
  Expected: PASS, 24 tests.

- [ ] **Step 4b: Create the entry point every message names**

```bash
#!/usr/bin/env bash
# The single implementation entry point (D6). Slash commands and the dead-upstream
# error body both route through this, so its path is load-bearing user-facing text.
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")/../proxy" && pwd)/chain.py" "$@"
```

```python
# tests/test_entry_point.py -- the file the error message names must exist and work.
import os, re, subprocess, unittest
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class EntryPointTest(unittest.TestCase):
    def test_chain_sh_exists_and_is_executable(self):
        path = os.path.join(REPO, "hooks", "chain.sh")
        self.assertTrue(os.path.exists(path))
        self.assertTrue(os.access(path, os.X_OK))

    def test_it_routes_to_the_python_implementation(self):
        out = subprocess.run(["bash", os.path.join(REPO, "hooks", "chain.sh"),
                              "is-self", "http://127.0.0.1:5588"],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0)

    def test_every_path_named_in_an_error_message_exists(self):
        # The dead-upstream body names hooks/chain.sh. If that ever moves, this fails.
        with open(os.path.join(REPO, "proxy", "server.py"), encoding="utf-8") as f:
            src = f.read()
        for rel in set(re.findall(r"hooks/[a-z-]+\.sh", src)):
            self.assertTrue(os.path.exists(os.path.join(REPO, rel)), f"{rel} is named but missing")
```

  `chmod +x hooks/chain.sh`.

- [ ] **Step 5: Commit**

```bash
git add proxy/chain.py hooks/chain.sh tests/test_chain_verb.py tests/test_unchain_verb.py \
        tests/test_status_verb.py tests/test_entry_point.py
git commit -m "feat(chain): chain, unchain and status verbs behind hooks/chain.sh"
```

---

### Task 7: Unfreeze the upstream — per-request resolution

Root bug #3. `server.py:100` resolves `UPSTREAM_URL` once at import and `start-proxy.sh` reuses a
live daemon, so the upstream freezes for the daemon's lifetime — a live `/health` showed
`upstream_url=https://api.anthropic.com` while headroom sat unused on `:8787`. Without this, `chain`
writes settings that the running proxy never reads, and R3 (no restart) is unreachable.

**Files:**
- Modify: `proxy/server.py:71-100` (`_load_upstream`, `UPSTREAM_URL`), `:123-124`
  (`_parsed_upstream`, `UPSTREAM_PATH`), `:149-163` (connection factory, full body), the reads at `:630` (a log line), `:634`,
  `:767`, `:865`, `:869`, `:1065`, and the response-header logging at `:642` and `:877`
- Modify: `proxy/compressor.py:38-40`, `:56-60`, `:445`, `:532`, `:583`, `:593` — the summarizer,
  which is the compaction path this plugin exists for and freezes exactly like `server.py` does
- Modify: `tests/_fakes.py:138` — `make_handler(body)` takes no `headers` argument today, and the
  loop-protection tests below pass one. Extend the signature to
  `make_handler(body: bytes, headers: dict | None = None)`, merging the supplied names into the
  `Message()` it already builds. Without this the three loop tests fail with `TypeError` before
  reaching any loop-protection logic.
- Test: `tests/test_upstream_reaches_socket.py`, `tests/test_server_upstream.py`,
  `tests/test_dead_upstream.py`, `tests/test_summarizer_follows.py`,
  `tests/test_loop_protection.py`, `tests/test_response_header_logging.py`,
  `tests/test_health_chain_fields.py`

**Interfaces:**
- Consumes: `chain.is_self`, `chain.read_settings` (Tasks 2–3).
- Produces: `Upstream` namedtuple (`scheme`, `host`, `port`, `path`) and
  `current_upstream() -> Upstream`, cached on `~/.claude/settings.json` `mtime_ns` + size.

- [ ] **Step 1: Write the failing test**

  This is the one test that fails against a fix that re-parses but never reaches the socket. Every
  other test in this plan would pass against that broken fix.

```python
"""The upstream a REQUEST actually reaches, with no daemon restart (spec section 7).

A string-only fix leaves _parsed_upstream, UPSTREAM_PATH and the connection factory frozen, so this
test drives real sockets rather than asserting on a resolved value.

Run: python3 -m unittest discover -s tests
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

# Every test file in this repo inserts proxy/ before importing server -- see tests/_fakes.py:25.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "proxy"))

import server

A, B = 5951, 5952


def _listener(port, hits):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            hits.append(port)
            body = b'{"type":"message","role":"assistant","content":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class UpstreamReachesSocketTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="socket-")
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        patch = mock.patch.dict(os.environ, {"HOME": self.home}, clear=False)
        patch.start()
        # Hermetic: this machine may export ANTHROPIC_BASE_URL (headroom does).
        # patch.dict restores the whole mapping on stop, so these pops are undone with it.
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        os.environ.pop("ROLLING_CONTEXT_UPSTREAM", None)
        os.environ.pop("ROLLING_CONTEXT_PORT", None)
        self.addCleanup(patch.stop)
        self.hits = []
        self.servers = [_listener(A, self.hits), _listener(B, self.hits)]
        for s in self.servers:
            self.addCleanup(s.server_close)
            self.addCleanup(s.shutdown)

    def _point_at(self, port):
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"env": {"ROLLING_CONTEXT_UPSTREAM": f"http://127.0.0.1:{port}"}}, f)

    def _one_request(self):
        """Drive the real request path, not a parallel one built for the test.

        tests/_fakes.py::make_handler is what the existing suite uses to invoke ProxyHandler
        against a captured socket; reuse it so this test exercises the same code a live request does.
        """
        from _fakes import make_handler
        body = json.dumps({"model": "claude-opus-5", "max_tokens": 1,
                           "messages": [{"role": "user", "content": "hi"}]}).encode()
        handler = make_handler(body)
        handler.path = "/v1/messages"
        handler.do_POST()

    def test_second_request_follows_a_changed_upstream_without_restart(self):
        self._point_at(A)
        self._one_request()
        self._point_at(B)
        self._one_request()
        self.assertEqual(self.hits, [A, B])

    def test_connection_factory_uses_the_live_value(self):
        self._point_at(B)
        up = server.current_upstream()
        self.assertEqual((up.host, up.port), ("127.0.0.1", B))


if __name__ == "__main__":
    unittest.main()
```

  `make_handler` already exists in `tests/_fakes.py` and is how the current suite drives
  `ProxyHandler`. If its captured-socket plumbing needs a small extension to reach a real upstream
  connection, extend it — do not add a parallel request path that only the test uses, because a
  parallel path would pass while the real one stays frozen.

- [ ] **Step 2: Run and verify it fails**

  Run: `python3 -m unittest tests.test_upstream_reaches_socket -v`
  Expected: FAIL — the second request lands on A, because the upstream is frozen at import.

- [ ] **Step 3: Replace import-time resolution**

  In `proxy/server.py`, delete the module-level `UPSTREAM_URL`, `_parsed_upstream` and
  `UPSTREAM_PATH` constants and add:

`server.py` runs with `proxy/` as its working directory (`hooks/start-proxy.sh` does
`cd "$PROXY_DIR"`), so `chain` is a sibling module — import it plainly, and add the import at the
top of the file alongside the existing ones:

```python
import chain            # sibling module: proxy/chain.py
import collections

Upstream = collections.namedtuple("Upstream", "scheme host port path")


class UpstreamRefused(Exception):
    """A file-sourced upstream is not loopback (D18). The request gets the dead-upstream error
    shape naming the offending file, rather than the key being forwarded off-machine."""

    def __init__(self, url, path):
        super().__init__(f"refusing to use chained upstream {url} from {path}")
        self.url = url
        self.path = path

_UPSTREAM_CACHE = {"stamp": None, "value": None}


def _settings_stamp():
    try:
        st = os.stat(chain.user_settings_path())
        return st.st_mtime_ns, st.st_size
    except FileNotFoundError:
        return None


def current_upstream():
    """Resolve per request, cached on the settings file's mtime and size.

    Returns a parsed struct, not a string: a string accessor fixes the literal UPSTREAM_URL sites
    and leaves _parsed_upstream, UPSTREAM_PATH and the connection factory frozen -- which is the
    bug, not the symptom.
    """
    stamp = _settings_stamp()
    if _UPSTREAM_CACHE["value"] is not None and _UPSTREAM_CACHE["stamp"] == stamp:
        return _UPSTREAM_CACHE["value"]

    raw = os.environ.get("ROLLING_CONTEXT_UPSTREAM")
    from_env = bool(raw)
    if not raw:
        env = (chain.read_settings(chain.user_settings_path()).get("env") or {})
        raw = env.get("ROLLING_CONTEXT_UPSTREAM")
        if not raw:
            candidate = env.get("ANTHROPIC_BASE_URL")
            raw = candidate if candidate and not chain.is_self(candidate) else None
    raw = raw or "https://api.anthropic.com"

    parsed = urlparse(raw)
    if chain.is_self(raw):
        parsed = urlparse("https://api.anthropic.com")
    # D18: a file-sourced upstream must be loopback. An exported variable is the user acting
    # deliberately in their own shell and is exempt.
    # A malformed file-sourced value parses to hostname=None. That is exactly the hand-edit D18
    # exists to catch, so it must refuse rather than fall through to Upstream(host=None) and die
    # later as an unhandled exception.
    if not from_env and parsed.hostname != "api.anthropic.com" \
            and not chain.host_matches(parsed.hostname, "127.0.0.1"):
        raise UpstreamRefused(raw, chain.user_settings_path())

    value = Upstream(parsed.scheme,
                     parsed.hostname,
                     parsed.port or (443 if parsed.scheme == "https" else 80),
                     parsed.path or "")
    _UPSTREAM_CACHE.update(stamp=stamp, value=value)
    return value
```

  Then replace every former constant read — `:634`, `:767`, `:865`, `:869`, `:1065` and the
  connection factory at `:151-161` — with a `current_upstream()` call, and build the socket from
  `up.scheme`/`up.host`/`up.port`.

- [ ] **Step 4: Follow the summarizer**

  `compressor.py:38-40` derives `SUMMARIZER_BASE_URL` at import and `:56-60` freezes host, port,
  scheme and path; `:445`, `:532`, `:583`, `:593` build request paths from the frozen value. Resolve
  at call time through the same accessor, unless `ROLLING_CONTEXT_SUMMARIZER_URL` is set, in which
  case that override stays authoritative (`SUMMARIZER_URL_SET`).

  This is not optional polish: the summarizer *is* the compaction path. A frozen summarizer URL sends
  compaction traffic to the old upstream while requests go to the new one — the feature silently
  half-working, which is the class of failure this whole design exists to end.

```python
# tests/test_summarizer_follows.py
def test_summarizer_follows_a_changed_upstream(self):
    self._point_at(A)
    self.assertEqual(compressor.summarizer_endpoint().port, A)
    self._point_at(B)
    self.assertEqual(compressor.summarizer_endpoint().port, B)

def test_explicit_override_still_wins(self):
    with mock.patch.dict(os.environ,
                         {"ROLLING_CONTEXT_SUMMARIZER_URL": "http://127.0.0.1:7777"}):
        self._point_at(B)
        self.assertEqual(compressor.summarizer_endpoint().port, 7777)
```

- [ ] **Step 4b: Turn refusals and dead upstreams into the documented error body**

  `UpstreamRefused` and a connection-level failure must both reach the client as the D9
  Anthropic-shaped body, not as an unhandled exception in `BaseHTTPRequestHandler`. Wrap the forward
  path where the outbound connection is made and where `current_upstream()` is called:

```python
def _api_error(message):
    return json.dumps({"type": "error",
                       "error": {"type": "api_error", "message": message}}).encode()


def _upstream_error_body(exc):
    """D9: Claude Code renders this as a message rather than a transport crash."""
    resolved = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if isinstance(exc, UpstreamRefused):
        return _api_error(
            f"rolling-context: refusing to use chained upstream {exc.url} from {exc.path} — "
            f"it is not a loopback address, and forwarding there would send your API key "
            f"off-machine. Fix or remove that value.")
    up = _UPSTREAM_CACHE.get("value")
    where = f"{up.scheme}://{up.host}:{up.port}" if up else "the chained upstream"
    if os.environ.get("ROLLING_CONTEXT_UPSTREAM"):
        return _api_error(
            f"rolling-context: chained upstream {where} is not answering. It comes from "
            f"ROLLING_CONTEXT_UPSTREAM in this session's environment, so unchain cannot clear it — "
            f"unset ROLLING_CONTEXT_UPSTREAM in your shell and restart the proxy.")
    return _api_error(
        f"rolling-context: chained upstream {where} is not answering. "
        f"Run: bash {resolved}/hooks/chain.sh unchain")
```

  The message names the **shell** form deliberately: the model cannot answer while its own requests
  are failing, so the escape hatch must not need a model round-trip. `<resolved>` comes from the
  running server's own location, never a hardcoded path — the plugin may be a symlink.

```python
# tests/test_dead_upstream.py
def test_connection_refused_returns_an_anthropic_shaped_body(self):
    self._point_at(59999)          # nothing listening
    handler = make_handler(b'{"model":"claude-opus-5","messages":[],"max_tokens":1}')
    handler.do_POST()
    body = json.loads(handler.wfile.getvalue().split(b"\r\n\r\n", 1)[1])
    self.assertEqual(body["error"]["type"], "api_error")
    self.assertIn("not answering", body["error"]["message"])

def test_tier_one_wording_names_the_variable_not_unchain(self):
    with mock.patch.dict(os.environ, {"ROLLING_CONTEXT_UPSTREAM": "http://127.0.0.1:59999"}):
        handler = make_handler(b'{"model":"claude-opus-5","messages":[],"max_tokens":1}')
        handler.do_POST()
        body = json.loads(handler.wfile.getvalue().split(b"\r\n\r\n", 1)[1])
        self.assertIn("ROLLING_CONTEXT_UPSTREAM", body["error"]["message"])
        self.assertNotIn("chain.sh unchain", body["error"]["message"])

def test_a_live_upstream_status_passes_through_untouched(self):
    self._point_at(A)              # listener answering 200
    handler = make_handler(b'{"model":"claude-opus-5","messages":[],"max_tokens":1}')
    handler.do_POST()
    self.assertIn(b"200", handler.wfile.getvalue().split(b"\r\n", 1)[0])

# tests/test_server_upstream.py
def test_non_loopback_at_tier_two_is_refused_and_names_the_file(self):
    self._write_user_settings({"ROLLING_CONTEXT_UPSTREAM": "https://proxy.example.com"})
    with self.assertRaises(server.UpstreamRefused) as ctx:
        server.current_upstream()
    self.assertEqual(ctx.exception.path, chain.user_settings_path())

def test_the_same_value_at_tier_one_is_honoured(self):
    with mock.patch.dict(os.environ,
                         {"ROLLING_CONTEXT_UPSTREAM": "https://proxy.example.com"}):
        self.assertEqual(server.current_upstream().host, "proxy.example.com")

def test_cache_invalidates_on_mtime_change(self):
    self._point_at(A)
    self.assertEqual(server.current_upstream().port, A)
    self._point_at(B)
    self.assertEqual(server.current_upstream().port, B)

def test_a_malformed_file_sourced_value_is_refused_not_forwarded(self):
    self._write_user_settings({"ROLLING_CONTEXT_UPSTREAM": "not-a-url"})
    with self.assertRaises(server.UpstreamRefused):
        server.current_upstream()

def test_our_own_address_falls_through_to_the_default_api(self):
    self._write_user_settings({"ROLLING_CONTEXT_UPSTREAM": f"http://127.0.0.1:{server.LISTEN_PORT}"})
    self.assertEqual(server.current_upstream().host, "api.anthropic.com")
```

- [ ] **Step 4c: `/health` fields and drift detection**

  `/health` gains `chained` and `upstream_reachable` beside the sanitized `upstream_url` and
  `upstream_source`, so `status` reads them rather than duplicating probe logic. The URL is
  re-serialized from the parsed `Upstream`, never echoed raw.

  Drift detection belongs to the hook, not here — `SessionStart` is what compares the live
  `ROLLING_CONTEXT_UPSTREAM` against `upstream.wrote`. Task 8 owns it, being the only task that
  touches `hooks/start-proxy.sh`.

```python
# tests/test_health_chain_fields.py
def test_health_exposes_chained_and_reachable(self):
    data = _health_json()
    self.assertIn("chained", data)
    self.assertIn("upstream_reachable", data)

def test_health_url_is_reserialized_not_echoed(self):
    self._write_user_settings({"ROLLING_CONTEXT_UPSTREAM": "http://127.0.0.1:8787/../x"})
    self.assertNotIn("..", _health_json()["upstream_url"])

```

- [ ] **Step 5: Loop protection and response-header filtering**

  Both are small and both are in spec §7. Every forwarded request carries
  `X-Rolling-Context-Chained-From: <our scheme>://<our host>:<our port>`, built from the live bind
  address; an inbound request already carrying that header naming *us* is refused as a loop rather
  than forwarded. `is_self` alone catches only a direct self-chain, not a cycle through an
  intermediate proxy. Compare with the same `host_matches`/`port_matches` normalization `is_self`
  uses — a raw string compare misses `localhost` against `127.0.0.1`.

  Response-side header logging at `:642` and `:877` logs header *values* at DEBUG; the request side
  at `:446`/`:794` is already name-only. Apply the same filter to both — a chained upstream is
  attacker-influenceable in a way `api.anthropic.com` is not.

```python
# tests/test_loop_protection.py
def test_our_own_address_in_the_header_is_refused_as_a_loop(self):
    handler = make_handler(b"{}", headers={"X-Rolling-Context-Chained-From":
                                           f"http://127.0.0.1:{server.LISTEN_PORT}"})
    handler.do_POST()
    self.assertIn(b"loop", handler.wfile.getvalue().lower())

def test_alternate_loopback_spelling_is_still_caught(self):
    handler = make_handler(b"{}", headers={"X-Rolling-Context-Chained-From":
                                           f"http://localhost:{server.LISTEN_PORT}"})
    handler.do_POST()
    self.assertIn(b"loop", handler.wfile.getvalue().lower())

def test_a_different_chained_from_address_forwards_normally(self):
    handler = make_handler(b"{}", headers={"X-Rolling-Context-Chained-From":
                                           "http://127.0.0.1:9999"})
    handler.do_POST()
    self.assertNotIn(b"loop", handler.wfile.getvalue().lower())
```

- [ ] **Step 6: Run and verify it passes, then run everything**

  Run: `python3 -m unittest tests.test_upstream_reaches_socket tests.test_server_upstream \
    tests.test_dead_upstream tests.test_summarizer_follows tests.test_loop_protection \
    tests.test_response_header_logging tests.test_health_chain_fields -v`
  Expected: PASS — requests land on A then B, no restart.
  Run: `python3 -m unittest discover -s tests`
  Expected: the whole suite green, including the pre-existing compression tests.

- [ ] **Step 7: Commit**

```bash
git add proxy/server.py proxy/compressor.py hooks/start-proxy.sh \
        tests/test_upstream_reaches_socket.py tests/test_server_upstream.py \
        tests/test_dead_upstream.py tests/test_summarizer_follows.py \
        tests/test_loop_protection.py tests/test_response_header_logging.py \
        tests/test_health_chain_fields.py
git commit -m "fix(proxy): resolve upstream per request, not at import

server.py:100 froze the upstream for the daemon's lifetime, so a live proxy
kept forwarding to api.anthropic.com while a chained proxy sat unused. The
accessor returns a parsed struct because the derived values -- _parsed_upstream,
UPSTREAM_PATH and the connection factory -- were frozen too."
```

---

### Task 8: The hook — stop mistaking a foreign proxy for ourselves, and say so

Root bugs #1 and #2, plus R1. `start-proxy.sh:63`'s `elif "127.0.0.1" not in existing` treats any
loopback address as us, which is exactly how headroom on `:8787` was mistaken for our own proxy. The
hook also reads only `~/.claude/settings.json`, while `headroom wrap claude` writes
`ANTHROPIC_BASE_URL` into the project's `settings.local.json` — so it never sees the displacing
value and prints "already".

**Files:**
- Modify: `hooks/start-proxy.sh:59-66`, `install.sh:59-66`
- Test: `tests/test_hook_output.py`, `tests/test_install_seeding.py`,
  `tests/test_upstream_drift.py`

**Interfaces:**
- Consumes: `chain.py is-self` (CLI, Task 2), `chain.effective` (Task 3).

- [ ] **Step 1: Write the failing tests**

```python
"""SessionStart emits exactly one JSON object, or nothing (Fact 2), and alerts on displacement.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO, "hooks", "start-proxy.sh")
FOREIGN = "http://127.0.0.1:8787"


class HookOutputTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="hook-home-")
        self.project = tempfile.mkdtemp(prefix="hook-proj-")
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        os.makedirs(os.path.join(self.project, ".claude"), exist_ok=True)

    def _run(self):
        env = dict(os.environ, HOME=self.home, ROLLING_CONTEXT_NO_START="1")
        return subprocess.run(["bash", HOOK], cwd=self.project, env=env,
                              capture_output=True, text=True, timeout=30)

    def _displace(self):
        with open(os.path.join(self.project, ".claude", "settings.local.json"), "w") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": FOREIGN}}, f)

    def test_displacement_emits_one_json_object_with_both_fields(self):
        self._displace()
        out = self._run().stdout.strip()
        payload = json.loads(out)          # exactly one object, or this raises
        self.assertIn("systemMessage", payload)
        self.assertIn("hookSpecificOutput", payload)
        self.assertIn("8787", payload["systemMessage"])

    def test_a_project_local_displacement_is_seen_at_all(self):
        # Root bug #2: the hook read only ~/.claude/settings.json and printed "already".
        self._displace()
        self.assertIn("8787", self._run().stdout)

    def test_loopback_foreign_proxy_is_not_mistaken_for_us(self):
        # Root bug #1: `elif "127.0.0.1" not in existing` treated headroom as ourselves.
        self._displace()
        self.assertNotIn("already", self._run().stdout.lower())

    def test_quiet_when_we_are_in_the_path(self):
        with open(os.path.join(self.project, ".claude", "settings.local.json"), "w") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:5588"}}, f)
        self.assertEqual(self._run().stdout.strip(), "")

    def test_diagnostics_never_reach_stdout(self):
        self._displace()
        result = self._run()
        for line in result.stdout.splitlines():
            self.assertTrue(line.strip().startswith("{") or not line.strip())


if __name__ == "__main__":
    unittest.main()
```

```python
"""install.sh seeds ANTHROPIC_BASE_URL in three cases and never chains silently (spec section 9).

Run: python3 -m unittest discover -s tests
"""
import json
import os
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(REPO, "install.sh")
FOREIGN = "http://127.0.0.1:8787"


class InstallSeedingTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="install-")
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)

    def _run(self):
        env = dict(os.environ, HOME=self.home, ROLLING_CONTEXT_NO_START="1")
        return subprocess.run(["bash", INSTALL], env=env, capture_output=True, text=True,
                              timeout=60)

    def _user_env(self):
        path = os.path.join(self.home, ".claude", "settings.json")
        if not os.path.exists(path):
            return {}
        with open(path) as f:
            return json.load(f).get("env", {})

    def _set(self, value):
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path, "w") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": value}}, f)

    def test_absent_writes_ours(self):
        self._run()
        self.assertIn("5588", self._user_env()["ANTHROPIC_BASE_URL"])

    def test_ours_is_left_alone(self):
        self._set("http://127.0.0.1:5588")
        self._run()
        self.assertEqual(self._user_env()["ANTHROPIC_BASE_URL"], "http://127.0.0.1:5588")

    def test_foreign_writes_nothing_and_prints_guidance(self):
        self._set(FOREIGN)
        result = self._run()
        self.assertEqual(self._user_env()["ANTHROPIC_BASE_URL"], FOREIGN)
        self.assertIn("chain", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and verify they fail**

  Run: `python3 -m unittest tests.test_hook_output tests.test_install_seeding -v`
  Expected: FAIL — the hook prints "already" for a loopback foreign proxy and never inspects the
  project file.

- [ ] **Step 3: Replace the guard in `hooks/start-proxy.sh:59-66`**

  Resolve the effective value across scopes rather than reading one file, and use the shared
  predicate instead of a substring test:

```bash
effective=$(python3 "$PROXY_DIR/chain.py" effective-abu 2>/dev/null)
if [ -z "$effective" ]; then
    write_ours                       # our own file, user scope
elif python3 "$PROXY_DIR/chain.py" is-self "$effective"; then
    :                                # already ours, nothing to do, say nothing
else
    emit_displacement_alert "$effective"   # write nothing; the user runs chain
fi
```

  `emit_displacement_alert` writes exactly one JSON object to stdout — both `systemMessage` and
  `additionalContext`, per spec section 8 — and every diagnostic line goes to stderr and
  `$HOME/.claude/rolling-context-hook.log`.

  Note the citation covers `:59-68`: the `else: print("already")` branch at `:67-68` is part of the
  same guard and changes with it.

  Apply the same three cases to `install.sh:59-68`. Neither file writes
  `ROLLING_CONTEXT_UPSTREAM` any more; `chain` does that, explicitly and recorded.

- [ ] **Step 3a: Suppress a repeat alert, and detect drift**

  Two behaviours from spec §8 that the hook owns. Without the first, the alert fires every session
  forever — the user learns to ignore it, which is the same silence this feature exists to break,
  reached from the other direction.

  **Suppression (D8), keyed per `{project, url}`.** Headroom binds a fixed port, so a URL-only key
  would alert once in the lifetime of the install and stay silent in every project after the first.

```python
# proxy/chain.py -- the hook owns no state logic of its own.
def should_alert(project, url):
    """(alert?, kind) for this session. Records the pair when it decides to alert."""
    state = load_state()
    seen = {(a["project"], a["url"]) for a in state.get("alerted") or []}
    ours = (state.get("abu") or {}).get(project)
    if (project, url) not in seen:
        state.setdefault("alerted", []).append({"project": project, "url": url})
        save_state(state)
        return True, ("overwritten" if ours else "displaced")
    if ours:
        # We chained here and something took it back. Say so every time, in different words.
        return True, "overwritten"
    return False, None


def drifted():
    """(old, new) when ROLLING_CONTEXT_UPSTREAM changed underneath us, else None.

    Invisible to the displacement check: ANTHROPIC_BASE_URL still points at us, so from that
    angle nothing changed -- but we are now chaining somewhere the user did not choose.
    """
    state = load_state()
    upstream = state.get("upstream")
    if not upstream:
        return None
    live = (read_settings(user_settings_path()).get("env") or {}).get(USER_KEY)
    return None if live == upstream["wrote"] else (upstream["wrote"], live)
```

  The hook calls `chain.py should-alert <url>` and `chain.py drifted`, and emits at most one JSON
  object per session — the displacement alert, the "your chain was overwritten" variant, or the
  drift alert. Exact texts are in spec §8; route both verbs through `main()`.

```python
# tests/test_upstream_drift.py
def test_drift_alerts_while_we_are_still_in_the_path(self):
    self._chain_through(FOREIGN)
    self._set_user_upstream("http://127.0.0.1:9999")
    self.assertIn("changed outside", self._hook_stdout())

def test_no_drift_alert_when_the_value_is_still_ours(self):
    self._chain_through(FOREIGN)
    self.assertEqual(self._hook_stdout().strip(), "")

def test_a_second_unrelated_drift_is_not_suppressed(self):
    self._chain_through(FOREIGN)
    self._set_user_upstream("http://127.0.0.1:9999")
    self._hook_stdout()
    self._set_user_upstream("http://127.0.0.1:7777")
    self.assertIn("changed outside", self._hook_stdout())
```

  And in `tests/test_hook_output.py`, the three suppression rows:

```python
def test_the_second_session_is_silent(self):
    self._displace()
    self.assertIn("8787", self._run().stdout)
    self.assertEqual(self._run().stdout.strip(), "")

def test_a_different_project_is_alerted_for_the_same_url(self):
    # Precisely why the key is {project, url} and not url alone.
    self._displace()
    self._run()
    other = self._new_project_displaced_by(FOREIGN)
    self.assertIn("8787", self._run_in(other).stdout)

def test_a_displaced_chain_re_alerts_with_different_wording(self):
    self._displace()
    self._chain()
    self._displace()
    self.assertIn("overwritten", self._run().stdout)
```

- [ ] **Step 3b: Add the test seam both test files depend on**

  `tests/test_hook_output.py` and `tests/test_install_seeding.py` run these scripts for real, and
  without a seam every test run forks a background daemon and writes a PID file. Add an early exit to
  both scripts, immediately after the guard block and before any daemon spawn:

```bash
# Test seam: run the detection and seeding logic, then stop before starting anything.
if [ -n "${ROLLING_CONTEXT_NO_START:-}" ]; then
    exit 0
fi
```

  This is the one piece of production surface added for tests, and it is deliberate: the alternative
  is a test suite that spawns real daemons on every run. It is a guard, not a code path — nothing
  above it behaves differently.

- [ ] **Step 4: Run and verify they pass**

  Run: `python3 -m unittest tests.test_hook_output tests.test_install_seeding -v`
  Expected: PASS, 14 tests.

- [ ] **Step 5: Full suite and a real end-to-end check**

  Run: `python3 -m unittest discover -s tests`
  Expected: green.

  Then, by hand, the case this whole feature exists for:
  1. Start a local listener on `:8787` and point a scratch project's `settings.local.json` at it.
  2. Run the hook — expect the alert naming `:8787`.
  3. Run `python3 proxy/chain.py chain --yes` — expect both keys written.
  4. Run `python3 proxy/chain.py status` — expect `in path: yes` and `chained: yes`.
  5. Run `python3 proxy/chain.py unchain` — expect `:8787` restored to the project file.

- [ ] **Step 6: Commit**

```bash
git add hooks/start-proxy.sh install.sh tests/test_hook_output.py tests/test_install_seeding.py
git commit -m "fix(hook): detect a displacing loopback proxy and alert instead of claiming success

start-proxy.sh:63's `elif \"127.0.0.1\" not in existing` treated every loopback
address as ourselves, so headroom on :8787 read as 'already installed'. The hook
also read only ~/.claude/settings.json while `headroom wrap claude` writes the
project's settings.local.json, so it never saw the displacing value."
```

---

### Task 9: The slash commands the alert names, plus docs and version

The alert and `status` both print `fix: /rolling-context:chain`. That command has to exist, or the
single command of R2 fails at the moment it is offered. These are thin wrappers over the verbs Task 6
already built (D6) — the shell form stays the sole implementation.

**Files:**
- Create: `commands/chain.md`, `commands/unchain.md`, `commands/status.md`
- Modify: `README.md`, `.claude-plugin/plugin.json` (2.2.1 → 2.3.0)
- Create: `CHANGELOG.md`
- Test: `tests/test_commands_exist.py`

**Interfaces:**
- Consumes: the CLI verbs from Task 6.

- [ ] **Step 1: Write the failing test**

  The point is not that three files exist — it is that every command string the plugin prints to a
  user or a model actually resolves to one of them.

```python
"""Every command the alert or status text names must exist as a slash command (D6, spec section 4).

Run: python3 -m unittest discover -s tests
"""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sources():
    for rel in ("proxy/chain.py", "hooks/start-proxy.sh"):
        with open(os.path.join(REPO, rel), encoding="utf-8") as f:
            yield rel, f.read()


class CommandsExistTest(unittest.TestCase):
    def test_every_named_slash_command_has_a_file(self):
        named = set()
        for rel, text in _sources():
            named |= set(re.findall(r"/rolling-context:([a-z-]+)", text))
        self.assertTrue(named, "expected the alert or status text to name a slash command")
        for verb in sorted(named):
            path = os.path.join(REPO, "commands", f"{verb}.md")
            self.assertTrue(os.path.exists(path), f"{verb} is named but commands/{verb}.md is missing")

    def test_each_command_invokes_the_shell_implementation(self):
        for verb in ("chain", "unchain", "status"):
            with open(os.path.join(REPO, "commands", f"{verb}.md"), encoding="utf-8") as f:
                body = f.read()
            self.assertIn("chain.py", body, f"commands/{verb}.md must call the one implementation")

    def test_version_was_bumped(self):
        import json
        with open(os.path.join(REPO, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["version"], "2.3.0")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and verify it fails**

  Run: `python3 -m unittest tests.test_commands_exist -v`
  Expected: FAIL — `commands/chain.md is missing`, and the version assertion fails on `2.2.1`.

- [ ] **Step 3: Write the three wrappers**

  `commands/chain.md`:

```markdown
---
description: Put rolling-context back in the request path, chained through the proxy that displaced it
---

Run the chain verb and report exactly what it prints:

!`python3 "${CLAUDE_PLUGIN_ROOT}/proxy/chain.py" chain --yes`

If it refused, the message names the reason. Do not retry with different arguments — the refusal
reasons are deliberate, and each one names what the user should do instead.
```

  `commands/unchain.md`:

```markdown
---
description: Undo the chain, giving back the base URL that rolling-context displaced
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/proxy/chain.py" unchain`
```

  `commands/status.md`:

```markdown
---
description: Report whether rolling-context is in the request path and what it is chained through
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/proxy/chain.py" status`
```

- [ ] **Step 4: Update the docs and the version**

  `README.md` gains a section covering: what the alert means, `chain`/`unchain`/`status`, and that
  chaining is explicit and never automatic. `.claude-plugin/plugin.json` goes to `2.3.0` — the
  behaviour change is that the hook no longer writes `ROLLING_CONTEXT_UPSTREAM` for you. Create
  `CHANGELOG.md` with a `2.3.0` entry naming the three fixed defects.

- [ ] **Step 5: Run and verify it passes**

  Run: `python3 -m unittest tests.test_commands_exist -v`
  Expected: PASS, 3 tests.

- [ ] **Step 6: Commit**

```bash
git add commands/ README.md CHANGELOG.md .claude-plugin/plugin.json tests/test_commands_exist.py
git commit -m "feat(commands): add the slash commands the displacement alert names

The alert and status both print /rolling-context:chain. Shipping that text
without the command would fail R2 at the moment it fires."
```

---

### Task 10: Uninstall must unchain before it deletes itself

Before this feature, nothing rolling-context owned was ever written into a project-scope file, so
`uninstall.sh` never needed to clean one up. `do_chain` changes that: it writes
`ANTHROPIC_BASE_URL` into the project's `settings.local.json`. `uninstall.sh:42-51` removes the plugin
directory — which contains `chain.sh` — *before* the settings block at `:89-127` runs, and that block
reads only `$CLAUDE_DIR/settings.json` and never project files.

So: chain, then uninstall, and Claude Code is left pointing at a dead `:5588` with no API
connectivity at all. This plan introduces that path, so this plan closes it.

**Files:**
- Modify: `uninstall.sh:42-51` (ordering), `:89-127` (settings block), `:92-95` (silent skip),
  `:111` (the last unmigrated `is_self` site — spec §9 lists it explicitly)
- Modify: `uninstall.ps1` — the same reorder, shipped review-verified only (D3, `pwsh` absent)
- Test: `tests/test_uninstall.py`

**Interfaces:**
- Consumes: `chain.py unchain --all` from Task 6.

- [ ] **Step 1: Write the failing test**

```python
"""Uninstall must undo a chain before deleting the code that knows how to undo it.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOREIGN = "http://127.0.0.1:8787"


class UninstallTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="uninstall-home-")
        self.project = tempfile.mkdtemp(prefix="uninstall-proj-")
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        os.makedirs(os.path.join(self.project, ".claude"), exist_ok=True)
        self.local = os.path.join(self.project, ".claude", "settings.local.json")
        with open(self.local, "w", encoding="utf-8") as f:
            json.dump({"env": {"ANTHROPIC_BASE_URL": FOREIGN}}, f)
        self.addCleanup(shutil.rmtree, self.home, True)
        self.addCleanup(shutil.rmtree, self.project, True)

    def _env(self):
        return dict(os.environ, HOME=self.home, ROLLING_CONTEXT_NO_START="1")

    def _chain(self):
        subprocess.run(["python3", os.path.join(REPO, "proxy", "chain.py"), "chain", "--yes"],
                       cwd=self.project, env=self._env(), capture_output=True, timeout=30)

    def _local_env(self):
        with open(self.local, encoding="utf-8") as f:
            return json.load(f).get("env", {})

    def test_uninstall_after_chain_does_not_strand_the_project(self):
        self._chain()
        self.assertTrue("5588" in self._local_env()["ANTHROPIC_BASE_URL"])
        subprocess.run(["bash", os.path.join(REPO, "uninstall.sh")],
                       env=self._env(), capture_output=True, timeout=60)
        # The project must not be left pointing at a proxy that no longer exists.
        self.assertNotIn("5588", self._local_env().get("ANTHROPIC_BASE_URL", ""))

    def test_state_file_and_lock_are_removed(self):
        self._chain()
        subprocess.run(["bash", os.path.join(REPO, "uninstall.sh")],
                       env=self._env(), capture_output=True, timeout=60)
        state = os.path.join(self.home, ".claude", "rolling-context-proxy.json")
        self.assertFalse(os.path.exists(state))
        self.assertFalse(os.path.exists(state + ".lock"))

    def test_a_skipped_step_is_reported_not_silent(self):
        result = subprocess.run(["bash", os.path.join(REPO, "uninstall.sh")],
                                env=dict(self._env(), PATH="/nonexistent"),
                                capture_output=True, text=True, timeout=60)
        self.assertTrue(result.stdout.strip() or result.stderr.strip(),
                        "a skipped interpreter check must say so, not exit silently")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and verify it fails**

  Run: `python3 -m unittest tests.test_uninstall -v`
  Expected: FAIL — the project file still names `:5588` after uninstall, because `chain.sh` was
  deleted before anything could call it.

- [ ] **Step 3: Reorder `uninstall.sh`**

  `chain.sh unchain --all` runs **first**, before any file is removed. The existing `:109-125`
  handling of `ROLLING_CONTEXT_*` then finds nothing left to do, which is correct rather than
  redundant — without the ordering it would restore `ANTHROPIC_BASE_URL` to a possibly-dead port.
  Make the interpreter guard at `:92-95` report what it skipped instead of skipping silently under
  `set -e`. Remove `rolling-context-proxy.json`.

  **Migrate the predicate at `:111`.** It currently reads `if current and "127.0.0.1" in current:` —
  the same loopback-conflation as root bug #1, at the seventh site spec §9 lists. Leaving it has a
  concrete regression: after `unchain --all` restores a *user-scope* foreign proxy (a legitimate
  outcome, since `effective()` resolves across scopes and the displacing value need not be
  project-scoped), this predicate sees a loopback address, finds `upstream` already cleared, and
  takes the `del env["ANTHROPIC_BASE_URL"]` branch — deleting the address it just restored and
  stripping connectivity the user still had. Replace it with `chain.py is-self`, matching the other
  six sites.

- [ ] **Step 3b: `uninstall.ps1`, review-verified**

  Apply the same reorder there: `chain.py unchain --all` before any removal. This is not covered by
  the PowerShell deferral, and the plan previously claimed it was. The deferral's reasoning —
  "Windows users keep today's behaviour, which is the pre-existing bug" — is true of
  `start-proxy.ps1` and false here: chaining is **new**, `commands/chain.md` invokes
  `chain.py` with no OS gate, and `chain.py` is cross-platform. So a Windows user who runs
  `/rolling-context:chain` and later uninstalls is stranded on a dead `:5588` by a path this plan
  creates. Per D3 the change ships review-verified with `pwsh-absent` recorded in the commit body;
  what is not acceptable is leaving it unwritten.

- [ ] **Step 4: Run and verify it passes**

  Run: `python3 -m unittest tests.test_uninstall -v`
  Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add uninstall.sh tests/test_uninstall.py
git commit -m "fix(uninstall): unchain before deleting the code that performs the unchain

uninstall.sh:42-51 removed the plugin directory before the settings block at
:89-127, and that block never read project files. Once chain writes
ANTHROPIC_BASE_URL into a project's settings.local.json, that ordering leaves
Claude Code pointing at a dead :5588 with no API connectivity."
```

---

## Done when

- The precedence probe has run and §2 of the spec records what it measured.
- `python3 -m unittest discover -s tests` is green, pre-existing tests included.
- The manual end-to-end check in Task 8 Step 5 passes: a listener on `:8787` is detected, alerted,
  chained through, reported by `status`, and restored by `unchain`.
- A live `/health` shows the chained upstream rather than `https://api.anthropic.com`, with no daemon
  restart between the `chain` call and the check.
- `/rolling-context:chain` — the command the alert names — actually runs.
- Chaining and then uninstalling leaves the project's `ANTHROPIC_BASE_URL` usable, not pointed at a
  proxy that no longer exists.

## Deferred, and why it is safe to defer

| Deferred | Why |
|---|---|
| PowerShell detection and seeding parity (`start-proxy.ps1:46`, `install.ps1:52`) | `pwsh` is absent on this machine (D3), so these ship unverified or not at all. Windows users keep today's behaviour on these two paths — and here "pre-existing" is accurate: `start-proxy.ps1:46` already compares against our own port rather than a bare loopback substring, so it does not carry root bug #1. What it does carry is the old silent-write behaviour, which is a pre-existing divergence from D9/D14, not something this plan introduces. |

`uninstall.ps1` is **not** deferred, despite `pwsh` being absent. Chaining is new and has no OS
gate, so a Windows user can reach the stranded-on-`:5588` state that Task 10 exists to prevent. The
reorder ships review-verified there (Task 10 Step 3b) rather than being left out on a platform
technicality.

Nothing else is deferred. The slash commands were briefly deferred as "ergonomics" and that was wrong
— the alert names them, so shipping the alert without them would fail R2 at the moment it fires.
The uninstall ordering was briefly deferred as "independent" and that was also wrong — `chain`'s
project-scope write is what makes it reachable, so this plan owns it.
