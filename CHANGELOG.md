# Changelog

## 2.4.0

Five bug fixes, all found by mutation testing and adversarial review of the 2.3.0 work.

### Fixed

- A malformed `ROLLING_CONTEXT_UPSTREAM` no longer kills the proxy. `current_upstream()` parsed
  URLs unguarded, so a non-numeric port, an out-of-range port, or a malformed IPv6 literal raised
  `ValueError` out of both `/health` and startup — the daemon died before binding its socket.
  It now degrades through the same refusal path as every other bad upstream, and `/health` reports
  the problem instead of returning a traceback.
- The displacement alert no longer goes silent when it cannot write its state file. On a read-only
  `$HOME` the alert was lost entirely; it now fires and simply repeats next session.
- Native mode is resolved per request instead of being captured at import. The daemon is long-lived
  and gets reused, so the old flag outlived the configuration it described.
- Removed a redundant self-pointer guard in `current_upstream()`. The following block already
  produced its outcome, so it was unreachable in effect.

### Testing

- Both `is_self()` parse-failure branches are now covered; the originally reported CRLF trigger
  does not reach them, because Python strips `\r` and `\n` from URLs.
- `/health`'s `chained` field is pinned in both directions — the previous tests only ever observed
  one side, so the comparison could be inverted without any test noticing.
- The suite is hermetic against all six documented `ROLLING_CONTEXT_*` and `ANTHROPIC_BASE_URL`
  variables. Exporting any one of them used to turn six unrelated tests red.
- Three test files that could only run as part of the full suite now run standalone.

## 2.3.0

Fixes three defects around a proxy displacing Rolling Context from the request path:

- **Displacement went undetected.** The install/session guard classified any loopback address as
  "already installed" (`"127.0.0.1" not in existing`), so a foreign proxy on a different loopback
  port (e.g. another tool bound to `:8787`) was misread as Rolling Context itself. Compaction
  silently stopped running, with no indication anything was wrong. The guard now resolves
  `ANTHROPIC_BASE_URL` across every settings scope Claude Code reads and classifies it with one
  shared predicate; a `SessionStart` alert fires whenever a foreign proxy holds the value, once per
  project/URL pair, and again if the chain is later overwritten or its target drifts.
- **The alert named a command that didn't exist.** Both the alert and `status` output pointed at
  `/rolling-context:chain`, but no such slash command shipped, so the one fix the product offered
  could not be run. Added `/rolling-context:chain`, `/rolling-context:unchain` and
  `/rolling-context:status` as thin wrappers over the existing `proxy/chain.py` verbs.
- **Chaining was implicit at install time.** The installer used to write
  `ROLLING_CONTEXT_UPSTREAM` and rewrite `ANTHROPIC_BASE_URL` for you automatically whenever it
  found an existing value, an unrecorded change with no undo path. Chaining is now always
  explicit: the installer writes nothing and points at `/rolling-context:chain` instead.

None of `chain`, `unchain` or `status` require restarting the proxy or Claude Code — the proxy
resolves its upstream fresh on every request, so the effect lands on the very next one.
