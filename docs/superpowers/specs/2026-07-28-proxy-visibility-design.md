# Working alongside another proxy: visibility first, chaining on request

**Date:** 2026-07-28
**Status:** design approved, not implemented
**Supersedes:** the design implemented on `feat/upstream-chaining` (commits
`d987048..4abe452`), retained unmerged as a reference only.

## 1. Problem

Starting Claude Code with another proxy in front — `headroom wrap claude` is the
reported case — silently removes rolling-context from the request path. Context
compaction stops running and nothing says so.

The user's statement of the defect:

> the problem is that with `headroom wrap claude` the headroom proxy is added at
> start time. the user does not see that rolling-context's proxy may have been
> overwritten.

The defect is **invisible feature loss**. Chaining is the remedy, not the
requirement. The previous design inverted this: it chained automatically and
reported the fact only to the model.

### Requirements

| # | Requirement | Source |
|---|---|---|
| R1 | The user is told when rolling-context is out of the request path | reported defect |
| R2 | A single command puts it back | user, this session |
| R3 | Restarting Claude Code is not necessary | user, this session |
| R4 | Use the existing `ROLLING_CONTEXT_UPSTREAM` mechanism; do not invent a parallel one | user, earlier |
| R5 | Ship as a plugin-level feature, not a config workaround, wrapper script, or patch to the other tool | user, earlier |
| R6 | User intervention is acceptable and may simplify the feature | user, this session |

## 2. Measured facts

All three were unknown or unverified when the previous design was built, and all
three are load-bearing. Method in each case: throwaway project directory, local
listeners, no API contact, `~/.claude` semantically unchanged.

### Fact 1 — Claude Code re-reads `ANTHROPIC_BASE_URL` per request

Probe: `scratchpad/spike/reread2.py`. Listener A answered request #1 successfully
with a streamed `tool_use` turn; project settings were then rewritten to listener
B; every follow-up request carrying `tool_result` arrived at **B**. No error was
involved.

An earlier probe (`reread.py`) showed the same movement but only across retries
after a 401, which would have proved re-read on the failure path alone. Fact 1 is
the healthy-session result.

**Therefore R3 is satisfiable by a settings write alone.** Neither Claude Code
nor our daemon restarts.

### Fact 2 — `SessionStart` stdout reaches the model, not the user

Per the hooks reference (https://docs.claude.com/en/docs/claude-code/hooks,
indexed this session as `claude-code-hooks-reference`): for `SessionStart`, plain
stdout already reaches Claude;
the field shown to the user is `systemMessage`. Stdout must contain only the JSON
object, and exit-code signalling must not be mixed with JSON output.

**Therefore the alert the previous design emitted was never visible to the user.**
That, not missing automation, was the bug in it.

### Fact 3 — scope precedence

Measured in spike `Gemini-b9b.1`:

```
managed  >  project-local  >  project-shared  >  user  >  process-env
```

Process environment is the **weakest** scope. A foreign proxy that only sets child
environment therefore cannot displace our user-scope value. Displacement can only
originate in a settings *file* — which is the file we must write, at the scope we
found it. Where to write is not a free choice.

## 3. Decisions

| ID | Decision |
|----|----------|
| D5 | The daemon re-resolves its upstream per request. No control endpoint, no daemon restart. |
| D6 | The command ships as `hooks/chain.sh` (sole implementation) plus thin slash-command wrappers. |
| D7 | `unchain` is a strict byte-exact undo of our own writes. |
| D8 | Alert once per foreign URL, then stay quiet; a `status` command makes the silence recoverable. |
| D9 | A dead chained upstream fails the request with a clear error. Never reroute traffic the user did not ask for. |
| D10 | A second chain is allowed when the foreign URL matches the recorded upstream, and refused on genuine divergence. |
| D11 | Implement on a fresh branch from `d987048`, porting only what a failing test for the new behaviour justifies. |
| D12 | The chained upstream is recorded in `ROLLING_CONTEXT_UPSTREAM` in `~/.claude/settings.json` — the existing mechanism (R4) — not in a field of our own. |

Carried from earlier: **D3** — `pwsh` is absent on this machine, so all `.ps1`
changes ship review-verified only, recorded as `pwsh-absent`, stated in the commit
body, never with fabricated output.

### Governing rule

> **We never displace a foreign value unless the user asks.**

This replaces four scattered behaviours: automatic session-start chaining,
install-time chaining, automatic re-chaining, and automatic de-chaining.

### Dropped from the previous design

All of the following were downstream of one silent assumption — that we may write
files we do not own, unasked:

automatic chaining on detection; session-id gating; staleness detection; automatic
de-chaining; retention rules; the `chains` map; first-write-wins arbitration;
`ROLLING_CONTEXT_AUTOCHAIN`; per-project concurrent upstreams; byte-exact
uninstall restoration of state we never recorded.

## 4. Mechanism

```
claude ──> :5588 (rolling-context, compaction) ──> :8787 (foreign) ──> api.anthropic.com
```

Two keys, each in the file that already owns it, plus a record of what was
displaced. Immediate by Fact 1.

```
~/.claude/settings.json          env.ROLLING_CONTEXT_UPSTREAM = http://127.0.0.1:8787
<project>/.claude/settings.local.json   env.ANTHROPIC_BASE_URL = http://127.0.0.1:5588
```

The first is the shipped mechanism (R4, D12), now written explicitly by a command
rather than silently by a session hook. The second is the only value we set in a
file we do not own, and the only reason a state file exists at all.

### Surface

| File | Role | Change |
|---|---|---|
| `proxy/chain.py` | detection, decisions, state I/O, three verbs, `is-self` | rewrite, much smaller |
| `hooks/chain.sh` | the command; sole implementation entry point | new |
| `commands/chain.md`, `commands/unchain.md`, `commands/status.md` | slash wrappers | new, directory does not exist yet |
| `hooks/start-proxy.sh` | `systemMessage` alert; three-case seeding | output contract changes |
| `proxy/server.py` | per-request upstream; error on dead upstream | change |
| `proxy/compressor.py` | summarizer follows the upstream | change |
| `install.sh`, `uninstall.sh` | three-case seeding; call `unchain --all` first | change |
| `*.ps1` | parity, review-verified only (D3) | change |
| `.claude-plugin/plugin.json` | 2.2.1 → 2.3.0 | change |
| `README.md`, `CHANGELOG.md` | behaviour change; changelog does not exist yet | change / new |

The plugin name in `.claude-plugin/plugin.json` is `rolling-context`, so the slash
commands are `/rolling-context:chain`, `:unchain`, `:status`. `install.sh:95,101`
links the plugin at `$HOME/.claude/plugins/rolling-context`.

## 5. State

`$HOME/.claude/rolling-context-proxy.json`

```json
{
  "version": 1,
  "writes": [
    {
      "path": "/home/dd/.claude/settings.json",
      "key": "ROLLING_CONTEXT_UPSTREAM",
      "wrote": "http://127.0.0.1:8787",
      "displaced": null
    },
    {
      "path": "/home/dd/proj/A/.claude/settings.local.json",
      "key": "ANTHROPIC_BASE_URL",
      "wrote": "http://127.0.0.1:5588",
      "displaced": "http://127.0.0.1:8787"
    }
  ],
  "alerted": ["http://127.0.0.1:8787"]
}
```

Two fields:

- **`writes`** — one entry per key we set, in the order we set it. `wrote` powers
  the read-back guard; `displaced` powers strict undo (D7). `"displaced": null`
  means the key was absent before us, so undo deletes it. `unchain` walks the list
  in reverse.
- **`alerted`** — foreign URLs already announced (D8).

**No `upstream` field** (D12) — the chained upstream is `ROLLING_CONTEXT_UPSTREAM`
in `~/.claude/settings.json`, which is also the value the daemon and
`uninstall.sh:109-125` already read.

**No `session_id`, no timestamps.** Both existed to support automatic de-chaining
and staleness, which are gone. Timestamps also produced the same-second tie that
made "newest wins" undeliverable in review round 5.

**No migration.** The v1 journal and v2 `chains` map existed only on
`feat/upstream-chaining`; shipped 2.2.1 writes no state file. The filename is new,
so any branch-era leftover is ignored. `_from_v1`, `_migrate_v1_locked` and
`V1MigrationTest` are deleted, not ported.

### Writing rules

Each traces to a specific past failure:

- `fcntl.flock` retained — every session's `SessionStart` may append to `alerted`,
  so the race is real.
- Atomic `tmp` + `os.replace`.
- **Unparseable JSON → refuse and report, never overwrite.** Review round 4 found a
  fallback to `{}` that destroyed an entire settings file. Applies to the state
  file and to every settings file we touch.
- Settings files are read, mutated in memory, and written back whole — never
  regenerated.

## 6. Verbs

Shared resolution: walk scopes in the Fact 3 order and return the winning value
**and the file it came from**. Displaced = the winner is not ours.

### `chain`

Guards, in order. Each refuses with a named reason and writes nothing; the two
no-ops exit 0.

| Reason | Condition | Exit |
|---|---|---|
| `not-displaced` | the winner is already ours | 0 |
| `nothing-to-chain` | the winner is the default API; no foreign proxy | 0 |
| `managed-scope` | the foreign value lives in `managed-settings.json` — unwinnable | 2 |
| `same-port-different-host` | the foreign URL uses our port on another host | 2 |
| `divergent-chain` | `ROLLING_CONTEXT_UPSTREAM` in `~/.claude/settings.json` is set to a different URL (D10) | 2 |
| `upstream-pinned-by-env` | `ROLLING_CONTEXT_UPSTREAM` is set in the process environment | 2 |
| `unparseable-settings` | a target settings file or the state file is invalid JSON | 2 |

`divergent-chain` compares against the settings value only, and allows the matching
case: a second project wrapped by the same proxy URL needs the same upstream, so it
is harmless and permitted.

`upstream-pinned-by-env` exists because tier 1 outranks tier 2 (section 7). With the
variable exported, writing it to settings would change nothing, so `chain` refuses
and names the variable rather than appearing to succeed. This is the surviving arm
of the previous design's D4.

Apply, **upstream first, base URL second**:

1. Record both intended `writes` entries in the state file.
2. Write `ROLLING_CONTEXT_UPSTREAM` = the foreign URL to `~/.claude/settings.json`.
3. Write `ANTHROPIC_BASE_URL` = `http://127.0.0.1:$ROLLING_CONTEXT_PORT` to the
   file that displaced us.
4. Read both back. On mismatch, undo in reverse and report failure.

The order is not arbitrary. Reversing it would point Claude Code at us before we
know where to forward, and "no upstream recorded" resolves to the default API —
silently un-chaining the user, which D9 forbids.

### `unchain`

Scope: entries whose `path` lies inside the current project root (nearest ancestor
containing `.claude`), plus the `~/.claude` upstream entry that belongs to them.
Uninstall passes `--all`.

Per entry, in reverse order, **read back before writing**: if the current value
equals our `wrote`, restore `displaced` byte-exact, or delete the key when
`displaced` is null. If it differs — the other proxy's exit already removed it, or
the user edited it — skip, report, and leave the file alone. Drop the entry either
way.

This guard is also what prevents resurrecting a dead port: headroom deletes that
key when it exits (`wrap.py:1779-1781`), removing our value first, so `unchain`
correctly finds nothing to undo.

### `status`

The counterweight to D8's silence. Prints: our port and whether the daemon answers
`/health`; the effective base URL and **which file supplies it**; whether we are in
the path; the recorded upstream and whether it is reachable; recorded writes;
alerted URLs.

```
rolling-context: daemon up on :5588
in path:  no  -- :8787 wins, from /home/dd/proj/A/.claude/settings.local.json
chained:  no
compaction: OFF this session
fix: /rolling-context:chain
```

**Exit codes:** 0 success or no-op, 2 refused with a named reason, 1 internal
error. The previous 3/`stale-plan` and 4/`unparseable` codes collapse into 2; no
automation reads them.

**Out of scope:** chaining to a hand-named URL when nothing displaced us.

## 7. Proxy-side resolution

Replaces the module-import resolution at `server.py:100` — the frozen-upstream
defect — with resolution at request time. This is the whole of ticket
`Gemini-b9b.6`.

Order, every branch guarded by `is_self` so we can never route to ourselves:

1. `ROLLING_CONTEXT_UPSTREAM` from the process environment — the user's own
   override wins.
2. `ROLLING_CONTEXT_UPSTREAM` from `~/.claude/settings.json` — what `chain` wrote
   (D12).
3. `ANTHROPIC_BASE_URL` from settings, when it is not ours.
4. `https://api.anthropic.com`.

Tiers 1–4 are the shipped `_load_upstream` shape with the freeze removed and a
correct self-check, not a new mechanism.

**Caching:** `stat()` `~/.claude/settings.json` per request; re-read only when
`mtime_ns` or size change. Atomic `os.replace` makes the stat a reliable trigger.

**Dead upstream (D9).** Connection-level failure — refused, DNS failure,
unreachable — returns an Anthropic-shaped error body, so Claude Code renders it as
a message rather than a transport crash:

```json
{"type":"error","error":{"type":"api_error","message":
 "rolling-context: chained upstream http://127.0.0.1:8787 is not answering. Run: bash <resolved>/hooks/chain.sh unchain"}}
```

`<resolved>` is computed from the running server's own location, never hardcoded —
the plugin may be a symlink at `$HOME/.claude/plugins/rolling-context` or a
checkout elsewhere.

The message names the **shell** form, not the slash command: the model cannot
answer while its own requests are failing, so the escape hatch must not require a
model round-trip. When the upstream came from tier 1, the message names the
environment variable instead. HTTP statuses returned by a live upstream pass
through untouched; only failures to reach it produce this.

**Two consumers that must follow the upstream or fail silently:**

- **The summarizer.** `compressor.py:38-40` derives `SUMMARIZER_BASE_URL` from
  `ROLLING_CONTEXT_UPSTREAM` at import and `:56-60` freezes host, port, scheme and
  path. With a per-request upstream, a frozen summarizer URL sends compaction
  traffic to the wrong place — the feature being restored, failing quietly. It
  resolves at call time, unless `ROLLING_CONTEXT_SUMMARIZER_URL` is set, in which
  case that override stays authoritative (`SUMMARIZER_URL_SET`).
- **The nine `UPSTREAM_URL` string consumers** in `server.py` become calls to one
  accessor returning a plain string, so no call site starts handling a tuple.

`/health` gains `chained` and `upstream_reachable` beside the sanitized
`upstream_url` and `upstream_source`. `status` reads it rather than duplicating
probe logic.

## 8. Detection and alert

`SessionStart` stdout becomes exactly one JSON object, or nothing (Fact 2). Every
diagnostic line printed today moves to stderr and
`$HOME/.claude/rolling-context-hook.log`; `hooks.json` already discards stderr.
The same contract applies to `start-proxy.ps1` (D3).

On displacement:

```json
{"hookSpecificOutput":{"hookEventName":"SessionStart",
  "additionalContext":"rolling-context is out of the request path this session; context compaction is not running. The fix is /rolling-context:chain."},
 "systemMessage":"[rolling-context] compaction is OFF this session — another proxy (http://127.0.0.1:8787) holds ANTHROPIC_BASE_URL.\n  fix: /rolling-context:chain     check anytime: /rolling-context:status"}
```

Both fields, deliberately: `systemMessage` so the user sees it, `additionalContext`
so the model knows and can offer the fix rather than working in a degraded session
unaware. The text names the consequence, not the mechanism, and names `status` so
going quiet stays recoverable.

### Suppression (D8), with one refinement

| Situation | Alert? |
|---|---|
| foreign URL not in `alerted` | yes, then record it |
| foreign URL in `alerted`, no write recorded for that file | no |
| foreign URL in `alerted`, **a write is recorded for that file** | **yes** — our chain was displaced |

The third row uses different wording ("your chain was overwritten") and does not
depend on which tool did it. Silence there would recreate the original bug in a new
place.

There is no "everything is fine" line. Silence plus `status` is the contract.

## 9. Install, uninstall, and the predicate sites

### Three-case seeding

`hooks/start-proxy.sh:59-66` today performs, every session,
`ROLLING_CONTEXT_UPSTREAM = existing; ANTHROPIC_BASE_URL = ours` — an automatic,
unrecorded chain invisible to any undo. It becomes:

| Existing `ANTHROPIC_BASE_URL` | Action |
|---|---|
| absent | write ours (our own file, user scope) |
| ours | nothing |
| foreign | **write nothing**; alert per §8 |

Defect #1 dies here: `elif "127.0.0.1" not in existing` treated any loopback as
self, which is exactly how headroom on `:8787` was mistaken for us. The hook no
longer writes `ROLLING_CONTEXT_UPSTREAM`; `chain` does, explicitly and recorded.
`install.sh:59-66` gets the same three cases — a foreign value at install time
prints guidance instead of chaining silently.

Behaviour change: README, a new `CHANGELOG.md`, and
`.claude-plugin/plugin.json` **2.2.1 → 2.3.0**.

### Uninstall — four verified defects

1. `uninstall.sh:42-51` removes the plugin directory **before** the `:89-127`
   settings block, and `chain.sh` lives in that directory. `chain.sh unchain --all`
   must run first, before anything is deleted.
2. It reads only `$CLAUDE_DIR/settings.json` and never project files, so our
   project write would outlive the uninstall and point Claude Code at a dead
   `:5588`. Fixed by (1).
3. `:92-95`'s interpreter guard skips **silently**; with `set -e` at `:4` a failure
   can leave half-cleaned state. It must report what it skipped.
4. `rolling-context-proxy.json` and its lock must be removed. Existing `:109-125`
   handling of `ROLLING_CONTEXT_*` stays.

Ordering makes (1) and the existing `:109-125` restore complementary rather than
conflicting: `unchain --all` has already undone both keys, so `:109-125` finds no
`ROLLING_CONTEXT_UPSTREAM` and does nothing. Without that order it would restore
`ANTHROPIC_BASE_URL` to the other proxy's possibly-dead port. `test_uninstall.py`
must cover both orders to keep the dependency explicit.

### One predicate

Seven sites with four different semantics — `start-proxy.sh:63`,
`start-proxy.ps1:46`, `install.sh:63`, `install.ps1:52`, `uninstall.sh:111`,
`uninstall.ps1:101`, `server.py:93` — collapse to one implementation. Shell and
PowerShell call `python3 chain.py is-self <url>`.

`grep '127\.0\.0\.1'` **undercounts these sites**: the `.ps1` files store escaped
dots, which is how a site was missed before. Legitimate non-predicate literals:
`start-proxy.sh:11`, `install.sh:11`, `start-proxy.ps1:13`, `install.ps1:27`,
`server.py:1090` (bind address).

## 10. Verification

Every item RED first, and each new test checked against the old code or a mutant to
prove it can fail. Review round 5's finding 1 was "free to happen" precisely
because no test covered state-file version handling at all.

| Test file | What must fail first |
|---|---|
| `test_is_self.py` | the single predicate across all four semantics the seven sites had, including `:8787`-is-not-us |
| `test_effective_value.py` | Fact 3 precedence, and that the source *file* is reported |
| `test_chain_verb.py` | each refusal reason; the two exit-0 no-ops; upstream-before-base-URL order; reverse undo when read-back fails |
| `test_unchain_verb.py` | restore vs delete on `displaced: null`; skip when our value is gone; reverse order; project-root scoping; `--all` |
| `test_state_io.py` | atomic replace; refusal on unparseable state; flock under contention |
| `test_server_upstream.py` | all four tiers, each `is_self`-guarded; cache invalidation on `mtime_ns` change |
| `test_dead_upstream.py` | connection refused → Anthropic-shaped body; per-tier wording; the resolved path is the server's own; live upstream statuses pass through |
| `test_summarizer_follows.py` | the summarizer tracks a changed upstream; the explicit override still wins |
| `test_hook_output.py` | stdout is exactly one JSON object or empty; `systemMessage` present when displaced; the three-row suppression matrix; diagnostics never on stdout |
| `test_uninstall.py` | `unchain --all` before directory removal; skips reported; state file and lock removed |

Port from the old branch: `test_chain_write.py:238`
(`test_timeout_is_a_logged_noop_not_a_block`) kills a `threading.Lock` mutant that
the threaded barrier test is blind to (0/15). It must not be weakened.

**End-to-end, unmocked**, by the method that produced Facts 1 and 2 — real local
listeners, no API contact: a chained request must arrive at the foreign listener
with compaction applied, and a dead upstream must yield the guidance error.

Tests importing `_fakes` run via `python3 -m unittest discover -s tests`.

## 11. Rejected alternatives

| Alternative | Why rejected |
|---|---|
| An `upstream` field in our own state file | a second place recording what `ROLLING_CONTEXT_UPSTREAM` already means (R4); needs new uninstall code that already exists for the shipped key |
| Control endpoint on `:5588` (`POST /chain`) | a mutable control surface any local process could use to retarget traffic; in-memory state is lost on restart, so it needs a file anyway |
| Restart only our daemon | drops in-flight requests and does not fix the frozen upstream — merely freezes it later |
| `unchain` always restores byte-exact | writes back a dead port after the other proxy exits |
| `unchain` always deletes the key | discards a project endpoint the user configured before we ran |
| Automatic chaining on detection | the whole of the rejected design; requires writing files we do not own, unasked, which forces the journal, revert, ownership guards and retention rules |
| Alert on every displaced session | rejected in favour of once-per-URL plus `status` (D8) |
| Fall back to the API when the chained upstream is dead | routes traffic the user did not ask for (D9) |
| Newest chain wins across divergent projects | one daemon has one upstream and a request carries no project identity; taking over would silently mis-route the other project |
| Per-project concurrent upstreams | requires per-request routing the single daemon cannot provide |
| Patch `headroom wrap`, or use `ANTHROPIC_TARGET_API_URL` to put headroom outside | out of scope (R5) |
| Launcher script or documented config workaround | not a shipped feature (R5) |
| A `--home` CLI flag for test convenience | production surface added for tests; tests set and restore `HOME` |

## 12. Open items

None blocking implementation. Next step: `writing-plans`.
