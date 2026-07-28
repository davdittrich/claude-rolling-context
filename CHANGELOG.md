# Changelog

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
