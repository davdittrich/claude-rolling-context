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
