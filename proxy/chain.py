"""chain.py — every chain decision, verb, and write primitive lives here.

Imported as a library by proxy/server.py and the hooks; run as a CLI by the shell and PowerShell
wiring. Pure stdlib: it must import cleanly on Windows, where fcntl does not exist.

Spec: docs/superpowers/specs/2026-07-28-proxy-visibility-design.md
"""
import contextlib
import json
import os
import sys
import tempfile
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


def main(argv):
    if not argv:
        sys.stderr.write("usage: chain.py {is-self <url>|chain [--yes]|unchain [--all]|status}\n")
        return 1
    verb, rest = argv[0], argv[1:]
    if verb == "is-self":
        return 0 if (rest and is_self(rest[0])) else 1
    cwd = os.getcwd()
    if verb == "chain":
        unknown = [a for a in rest if a != "--yes"]
        if unknown:
            sys.stderr.write(f"unknown flag: {display(unknown[0])}\n")
            return 1
        root = project_root(cwd)
        if root is None:
            print("no project here — run this inside a project directory")
            return 2
        return do_chain(root, assume_yes="--yes" in rest)
    if verb == "unchain":
        unknown = [a for a in rest if a != "--all"]
        if unknown:
            sys.stderr.write(f"unknown flag: {display(unknown[0])}\n")
            return 1
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
    sys.stderr.write(f"unknown verb: {display(verb)}\n")
    return 1


def display(text):
    """Escape control and non-printable bytes for any message, log line, or state-file value.

    Structural safety only: no terminal escapes, no injected newlines, no corrupted JSON. A name
    written in plain printable text has nothing to escape and passes through unchanged -- the same
    residue section 7 accepts for URL path components.
    """
    if text is None:
        return ""
    return "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in str(text))
def state_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "rolling-context-proxy.json")


def empty_state():
    """abu: the key someone else owns, so it carries what we displaced, keyed by project path.
    upstream: our own key, so it carries only who is still chained through it -- no displaced
    value, because nothing else writes ROLLING_CONTEXT_UPSTREAM."""
    return {"abu": {}, "upstream": None, "alerted": []}


def load_state():
    """Absent file reads as empty_state(). Raises UnparseableSettings on bad JSON rather than
    ever treating a corrupt state file as empty and silently discarding it."""
    path = state_path()
    if not os.path.exists(path):
        return empty_state()
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise UnparseableSettings(path)


def save_state(state):
    """Atomic replace at mode 0600 -- it names project paths and local proxy topology.

    No lock: os.replace is the whole concurrency story (spec section 5). A lock would need
    fcntl, which does not exist on Windows and this module must import cleanly there.
    """
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
    upstream_value, upstream_source = effective(USER_KEY, project)
    if (upstream_value is not None and upstream_source != "<environment>"
            and state.get("upstream") is None and upstream_value != value):
        raise Refusal("upstream-already-set",
                      f"{USER_KEY} is already set to {display(upstream_value)} in "
                      f"{display(upstream_source)} and rolling-context did not put it there. "
                      f"refusing to overwrite it — remove it yourself if you want "
                      f"rolling-context to manage the upstream")
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
        # The environment is the weakest scope (Fact 3) and not a real settings file -- write
        # into the project-local file that outranks it instead. There is no file value to give
        # back, so record displaced=None; unchain's existing None-restores-by-deleting path
        # then removes the key instead of restoring a bogus value.
        write_target, displaced = source, url
        if source == "<environment>":
            write_target = os.path.join(project, ".claude", "settings.local.json")
            displaced = None

        # Keyed by project: D10 allows two chained at once, and an unkeyed record would let
        # B's chain overwrite A's, after which A's unchain would restore B's displaced value
        # into B's file -- un-chaining a project the user never named.
        state.setdefault("abu", {})[os.path.realpath(project)] = {
            "path": write_target, "wrote": ours, "displaced": displaced}
        state["upstream"] = {"wrote": url}
        save_state(state)

        # Upstream first: reversing this points Claude Code at us before we know where to
        # forward, and "no upstream recorded" resolves to the default API -- silently
        # un-chaining the user, which D9 forbids.
        _write_key(user_settings_path(), USER_KEY, url)
        _write_key(write_target, ABU_KEY, ours)

        # Read back the EFFECTIVE value, not the file we just wrote. A managed policy -- which
        # may arrive by plist, registry or drop-in, where no file check can see it -- leaves our
        # write in place while the value that actually applies stays foreign. Resolving again is
        # the only channel-agnostic way to know the write won.
        landed, landed_source = effective(ABU_KEY, project)
        if _read_key(user_settings_path(), USER_KEY) != url or not is_self(landed or ""):
            _write_key(write_target, ABU_KEY, displaced)
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
        did_anything = False
        for key in targets:
            abu = records[key]
            if _read_key(abu["path"], ABU_KEY) == abu["wrote"]:
                _write_key(abu["path"], ABU_KEY, abu["displaced"])
            else:
                print(f"skipped {display(abu['path'])} — {ABU_KEY} is no longer ours")
            del records[key]
            did_anything = True
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
            did_anything = True
        save_state(state)
        # D2/M5: an empty run is a legitimate no-op, but printing "unchained" for it is
        # indistinguishable from a real restore -- say plainly that nothing happened.
        print("unchained" if did_anything else "nothing recorded for this project — nothing to undo")
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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
