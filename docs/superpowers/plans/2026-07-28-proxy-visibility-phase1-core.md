# Proxy Visibility — Phase 1: Probe and `chain.py` Foundations

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Measure the one unverified assumption in the spec, then build the `chain.py` primitives every
later phase depends on — the `is-self` predicate, settings resolution, the state file, and path handling.

**Architecture:** One new module `proxy/chain.py`, importable as a library and runnable as a CLI. This
phase adds no verbs (`chain`/`unchain`/`status` are Phase 2) and touches no existing file except to record
a measured fact in the spec. Everything here is pure-stdlib Python 3, matching `proxy/server.py`.

**Tech Stack:** Python 3 stdlib only — `json`, `os`, `fcntl`/`msvcrt`, `urllib.parse`, `unittest`.

**Spec:** `docs/superpowers/specs/2026-07-28-proxy-visibility-design.md` (design-review-gate approved).
Section references below (§5, §6, §12) point into it.

## Global Constraints

- Pure stdlib. No new dependencies. `proxy/server.py` and `proxy/compressor.py` are stdlib-only today.
- Tests run via `python3 -m unittest discover -s tests` from the repo root.
- Test files live in `tests/`, named exactly as §10 of the spec names them.
- Never hardcode `5588`. The listen port comes from `ROLLING_CONTEXT_PORT` or defaults to `5588`,
  matching `proxy/server.py:47`.
- `fcntl` is POSIX-only. Any import of it must sit behind a platform check so `chain.py` imports cleanly
  on Windows (§5).
- Unparseable JSON is refused and reported, never overwritten — for the state file and every settings
  file (§5).
- The state file is written mode `0600` (§5).
- Every `project` path is `realpath`'d before it is stored and before any comparison (§5).
- Exit codes: `0` success or no-op, `2` refused with a named reason, `1` internal error (§6).

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
  - `SETTINGS_SCOPES` — the ordered scope list, highest precedence first.
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


def settings_scopes(project_root):
    """Files that can supply a value, highest precedence first (spec section 2, Fact 3)."""
    scopes = []
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
    """(value, source_path) for key, or (None, None). Process environment wins (spec section 7)."""
    from_env = os.environ.get(key)
    if from_env:
        return from_env, "<environment>"
    for path in settings_scopes(project_root):
        env_block = read_settings(path).get("env") or {}
        value = env_block.get(key)
        if value:
            return value, path
    return None, None
```

- [ ] **Step 4: Run and verify it passes**

  Run: `python3 -m unittest tests.test_effective_value -v`
  Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add proxy/chain.py tests/test_effective_value.py
git commit -m "feat(chain): resolve the effective value and report which file supplies it"
```

---

### Task 4: Path handling — canonicalization, escaping, write-target containment

Three rules from §5, all of which later tasks depend on and none of which are about the state file
itself. Escaping is a **structural** guarantee only: it stops terminal control sequences and broken
JSON, and does not neutralize a directory named in plain printable text (§5, §10).

**Files:**
- Modify: `proxy/chain.py`
- Test: `tests/test_path_sanitizing.py`

**Interfaces:**
- Produces:
  - `canonical(path) -> str` — `realpath`, applied before storage and before every comparison.
  - `display(text) -> str` — escapes control and non-printable bytes for any message, log line, or
    state-file value.
  - `write_target(project_root) -> str` — the resolved `settings.local.json` path.
  - `target_escapes_project(project_root) -> bool` — True when that path resolves outside the root.

- [ ] **Step 1: Write failing tests**

```python
"""Project paths are canonical before comparison and escaped before display (spec section 5).

Escaping is structural: control bytes cannot reach a terminal, a log line, or the state file. It is
NOT an anti-prompt-injection measure and is not tested as one -- printable text has nothing to escape.

Run: python3 -m unittest discover -s tests
"""
import os
import shutil
import tempfile
import unittest

from proxy import chain


class CanonicalTest(unittest.TestCase):
    def test_symlinked_project_resolves_to_its_real_path(self):
        real = tempfile.mkdtemp(prefix="canon-real-")
        link = os.path.join(tempfile.mkdtemp(prefix="canon-link-"), "alias")
        os.symlink(real, link)
        self.addCleanup(shutil.rmtree, real, True)
        self.assertEqual(chain.canonical(link), os.path.realpath(real))

    def test_relative_and_absolute_spellings_agree(self):
        real = tempfile.mkdtemp(prefix="canon-rel-")
        self.addCleanup(shutil.rmtree, real, True)
        cwd = os.getcwd()
        os.chdir(real)
        try:
            self.assertEqual(chain.canonical("."), chain.canonical(real))
        finally:
            os.chdir(cwd)


class DisplayTest(unittest.TestCase):
    def test_escape_sequences_cannot_reach_the_terminal(self):
        out = chain.display("/tmp/\x1b[31mred\x1b[0m")
        self.assertNotIn("\x1b", out)

    def test_newlines_cannot_break_a_log_line(self):
        out = chain.display("/tmp/a\nb")
        self.assertNotIn("\n", out)

    def test_printable_text_is_left_alone(self):
        # The honest limit: nothing here to escape. Recorded so nobody mistakes this for
        # an injection defense.
        plain = "/tmp/ignore previous instructions"
        self.assertEqual(chain.display(plain), plain)


class WriteTargetTest(unittest.TestCase):
    def setUp(self):
        self.project = tempfile.mkdtemp(prefix="target-")
        self.addCleanup(shutil.rmtree, self.project, True)
        os.makedirs(os.path.join(self.project, ".claude"), exist_ok=True)

    def test_ordinary_project_target_is_inside(self):
        self.assertFalse(chain.target_escapes_project(self.project))
        self.assertTrue(chain.write_target(self.project).startswith(chain.canonical(self.project)))

    def test_symlinked_dot_claude_escapes_and_is_detected(self):
        outside = tempfile.mkdtemp(prefix="target-outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        shutil.rmtree(os.path.join(self.project, ".claude"))
        os.symlink(outside, os.path.join(self.project, ".claude"))
        self.assertTrue(chain.target_escapes_project(self.project))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and verify it fails**

  Run: `python3 -m unittest tests.test_path_sanitizing -v`
  Expected: FAIL — `AttributeError: module 'proxy.chain' has no attribute 'canonical'`

- [ ] **Step 3: Write the minimal implementation**

  Append to `proxy/chain.py`:

```python
def canonical(path):
    """Resolved, symlink-free absolute path. Applied before storage and before every comparison."""
    return os.path.realpath(os.path.expanduser(path))


def display(text):
    """Escape control and non-printable bytes for any message, log line, or state-file value.

    Structural safety only: no terminal escapes, no injected newlines, no corrupted JSON. A name
    written in plain printable text has nothing to escape and passes through unchanged -- the same
    residue section 7 accepts for URL path components.
    """
    if text is None:
        return ""
    return "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in str(text))


def write_target(project_root):
    """The file chain writes ANTHROPIC_BASE_URL into, fully resolved."""
    return canonical(os.path.join(project_root, ".claude", "settings.local.json"))


def target_escapes_project(project_root):
    """True when .claude resolves outside the project -- e.g. a clone shipping it as a symlink.

    Canonicalizing the root does not constrain what sits beneath it, so this is checked separately
    and chain refuses on it (spec section 5, section 6).
    """
    root = canonical(project_root)
    return not (write_target(project_root) + os.sep).startswith(root + os.sep)
```

- [ ] **Step 4: Run and verify it passes**

  Run: `python3 -m unittest tests.test_path_sanitizing -v`
  Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add proxy/chain.py tests/test_path_sanitizing.py
git commit -m "feat(chain): canonicalize paths, escape them for display, contain the write target"
```

---

### Task 5: State file — schema, locking, atomic replace, mode 0600

The record of every key we set, so `unchain` can undo exactly what we did (§5). This task builds
read/write/lock only; `writes` and `shared_upstream` gain their semantics in Phase 2.

**Files:**
- Modify: `proxy/chain.py`
- Test: `tests/test_state_io.py`, `tests/test_state_version.py`

**Interfaces:**
- Produces:
  - `state_path() -> str` — `$HOME/.claude/rolling-context-proxy.json`.
  - `lock_path() -> str` — the same path with `.lock` appended.
  - `STATE_VERSION = 1`.
  - `empty_state() -> dict` — `{"version": 1, "writes": [], "alerted": []}`.
  - `load_state() -> dict` — raises `UnparseableSettings` on bad JSON, `UnsupportedStateVersion` on a
    version we do not know.
  - `save_state(state) -> None` — atomic `os.replace`, mode `0600`.
  - `locked()` — context manager holding an exclusive lock for a verb's whole sequence.
  - `UnsupportedStateVersion` — exception carrying `.found`.

- [ ] **Step 1: Write failing tests**

```python
"""State file I/O: atomic, locked, 0600, and refusing rather than overwriting (spec section 5).

Run: python3 -m unittest discover -s tests
"""
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock

from proxy import chain


class StateIOTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="state-")
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        patch = mock.patch.dict(os.environ, {"HOME": self.home}, clear=False)
        patch.start()
        self.addCleanup(patch.stop)

    def test_absent_state_reads_as_empty(self):
        self.assertEqual(chain.load_state(), chain.empty_state())

    def test_round_trip(self):
        state = chain.empty_state()
        state["writes"].append({"project": "/p", "path": "/f", "key": "K",
                                "wrote": "v", "displaced": None})
        chain.save_state(state)
        self.assertEqual(chain.load_state(), state)

    def test_written_mode_is_0600(self):
        chain.save_state(chain.empty_state())
        mode = stat.S_IMODE(os.stat(chain.state_path()).st_mode)
        self.assertEqual(mode, 0o600)

    def test_rewrite_keeps_mode_0600(self):
        chain.save_state(chain.empty_state())
        chain.save_state(chain.empty_state())
        mode = stat.S_IMODE(os.stat(chain.state_path()).st_mode)
        self.assertEqual(mode, 0o600)

    def test_no_temp_file_is_left_behind(self):
        chain.save_state(chain.empty_state())
        leftovers = [n for n in os.listdir(os.path.join(self.home, ".claude"))
                     if n.startswith("rolling-context-proxy.json.")]
        self.assertEqual(leftovers, [])

    def test_unparseable_state_refuses_rather_than_overwriting(self):
        with open(chain.state_path(), "w", encoding="utf-8") as f:
            f.write("{ broken")
        with self.assertRaises(chain.UnparseableSettings):
            chain.load_state()
        with open(chain.state_path(), encoding="utf-8") as f:
            self.assertEqual(f.read(), "{ broken")

    def test_lock_serializes_two_holders(self):
        order = []

        def hold(tag, delay):
            with chain.locked():
                order.append(f"{tag}-in")
                time.sleep(delay)
                order.append(f"{tag}-out")

        first = threading.Thread(target=hold, args=("a", 0.3))
        first.start()
        time.sleep(0.05)
        second = threading.Thread(target=hold, args=("b", 0.0))
        second.start()
        first.join()
        second.join()
        self.assertEqual(order, ["a-in", "a-out", "b-in", "b-out"])


class StateVersionTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="stateversion-")
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        patch = mock.patch.dict(os.environ, {"HOME": self.home}, clear=False)
        patch.start()
        self.addCleanup(patch.stop)

    def _write_raw(self, obj):
        with open(chain.state_path(), "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def test_current_version_round_trips(self):
        self._write_raw({"version": chain.STATE_VERSION, "writes": [], "alerted": []})
        self.assertEqual(chain.load_state()["version"], chain.STATE_VERSION)

    def test_newer_version_is_refused_not_coerced(self):
        self._write_raw({"version": chain.STATE_VERSION + 1, "writes": [], "alerted": []})
        with self.assertRaises(chain.UnsupportedStateVersion) as ctx:
            chain.load_state()
        self.assertEqual(ctx.exception.found, chain.STATE_VERSION + 1)

    def test_missing_version_is_refused(self):
        self._write_raw({"writes": [], "alerted": []})
        with self.assertRaises(chain.UnsupportedStateVersion):
            chain.load_state()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and verify both fail**

  Run: `python3 -m unittest tests.test_state_io tests.test_state_version -v`
  Expected: FAIL — `AttributeError: module 'proxy.chain' has no attribute 'load_state'`

- [ ] **Step 3: Write the minimal implementation**

  Append to `proxy/chain.py`. The lock import is platform-split so this module still imports on
  Windows, where `fcntl` does not exist and every `.ps1` caller would otherwise break.

```python
import contextlib
import tempfile

STATE_VERSION = 1

if os.name == "nt":
    import msvcrt

    def _lock(fh):
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _unlock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class UnsupportedStateVersion(Exception):
    """The state file claims a version we do not implement. Refuse rather than guess."""

    def __init__(self, found):
        super().__init__(f"state file version {found!r} is not supported")
        self.found = found


def state_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "rolling-context-proxy.json")


def lock_path():
    return state_path() + ".lock"


def empty_state():
    return {"version": STATE_VERSION, "writes": [], "alerted": []}


@contextlib.contextmanager
def locked():
    """Hold the exclusive lock for a verb's whole sequence -- guards, writes, read-backs, save.

    Held this widely on purpose: D10 permits concurrent chain calls from different projects, and a
    narrower scope lets two applies interleave their read-backs against each other's writes.
    """
    os.makedirs(os.path.dirname(lock_path()), exist_ok=True)
    fh = open(lock_path(), "a+")
    try:
        _lock(fh)
        yield
    finally:
        try:
            _unlock(fh)
        finally:
            fh.close()


def load_state():
    path = state_path()
    if not os.path.exists(path):
        return empty_state()
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise UnparseableSettings(path)
    if state.get("version") != STATE_VERSION:
        raise UnsupportedStateVersion(state.get("version"))
    return state


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

- [ ] **Step 4: Run and verify both pass**

  Run: `python3 -m unittest tests.test_state_io tests.test_state_version -v`
  Expected: PASS, 10 tests.

- [ ] **Step 5: Run the whole suite — nothing existing may regress**

  Run: `python3 -m unittest discover -s tests`
  Expected: all tests pass, including the pre-existing compression suite.

- [ ] **Step 6: Commit**

```bash
git add proxy/chain.py tests/test_state_io.py tests/test_state_version.py
git commit -m "feat(chain): state file with exclusive lock, atomic replace, 0600, version refusal"
```

---

## Phase 1 done when

- The precedence probe has run and §2 records what it measured.
- `python3 -m unittest discover -s tests` is green.
- `python3 chain.py is-self <url>` answers correctly for our own URL, headroom's, and a same-port
  different-host URL.
- No verb exists yet. `chain`, `unchain` and `status` are Phase 2, which consumes every interface above.
