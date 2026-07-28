"""chain.py — every chain decision, verb, and write primitive lives here.

Imported as a library by proxy/server.py and the hooks; run as a CLI by the shell and PowerShell
wiring. Pure stdlib: it must import cleanly on Windows, where fcntl does not exist.

Spec: docs/superpowers/specs/2026-07-28-proxy-visibility-design.md
"""
import json
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
    if len(argv) >= 2 and argv[0] == "is-self":
        return 0 if is_self(argv[1]) else 1
    sys.stderr.write("usage: chain.py is-self <url>\n")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))


def display(text):
    """Escape control and non-printable bytes for any message, log line, or state-file value.

    Structural safety only: no terminal escapes, no injected newlines, no corrupted JSON. A name
    written in plain printable text has nothing to escape and passes through unchanged -- the same
    residue section 7 accepts for URL path components.
    """
    if text is None:
        return ""
    return "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in str(text))
