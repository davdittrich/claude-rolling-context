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
| D13 | `chain` refuses any non-loopback foreign URL outright, no opt-out. Full request headers, including the API key, are forwarded to whatever `ROLLING_CONTEXT_UPSTREAM` names (`server.py:437-443`); the only use case in scope is a local proxy, so there is nothing to trade off. |
| D14 | Before the `~/.claude/settings.json` write — the scope-escalation step, since it becomes upstream for every project on the machine — `chain` prints the destination and an explicit statement that all API traffic, machine-wide, will route through it, and requires confirmation: interactive `y/N`, or `--yes` for scripted use. Still one command (R2): confirmation is a step inside running it, not a second command. |
| D15 | The per-request upstream accessor returns a small parsed struct (`scheme`, `host`, `port`, `path`), not a plain string. A string accessor only fixes the literal `UPSTREAM_URL` sites; it misses `_parsed_upstream`, `UPSTREAM_PATH`, and the connection factory (`server.py:123-124,151-161`), which is why a naive fix would not actually kill the frozen-upstream bug. |
| D16 | Loop protection beyond `is-self`: every chained forward carries `X-Rolling-Context-Chained-From: <our own scheme>://<our own host>:<our own port>`. An inbound request already carrying that header naming this daemon's own address is refused as a loop rather than forwarded — `is-self` alone only catches a direct self-chain, not a longer cycle through an intermediate proxy. The header is compared with the same `host_matches`/`port_matches` normalization `is-self` uses (§6), never as a raw string: an intermediate proxy that relays `localhost` where we bind `127.0.0.1` would otherwise walk straight through the check. The emitted value always uses the loopback form regardless of the actual bind host. |
| D17 | `ROLLING_CONTEXT_UPSTREAM` lives in one shared file while `writes` records it per project, so a `(path, key)` tuple no longer identifies one write. `writes` is append-per-project, the reference count is derived from the list itself, and only the last remaining reference restores — using the earliest recorded `displaced`. Without it, plain `unchain` in one project silently un-chains every other project chained to the same proxy, with no alert firing. See §5. |
| D18 | §7 enforces D13's loopback rule at resolution time as well, for the file-sourced tiers (2 and 3) only. `chain` is not the only thing that can put a value in `~/.claude/settings.json` — a hand-edit or a future writer bypasses its guards entirely, and §7 then forwards full headers including the API key (`server.py:437-443`) wherever that value points. Tier 1 — `ROLLING_CONTEXT_UPSTREAM` exported in the process environment — is exempt: that is a deliberate per-invocation act by the user, not a persistent file another tool can rewrite behind their back, and restricting it would break anyone deliberately pointing at a remote gateway. A refused file-sourced value produces the §7 dead-upstream error shape naming the reason, not a silent fallback. |

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

**Provenance.** D1–D12 come from the brainstorming session with the user. D13–D15
are user decisions taken during design-review-gate iteration 1 (loopback scope,
scope-escalation confirmation, accessor shape); D16–D17 answer blockers raised by
the Security and Architect reviewers in iterations 1 and 2 respectively; D18
answers the Security reviewer's iteration-2 question, decided by the user.

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
      "project": "/home/dd/proj/A",
      "path": "/home/dd/.claude/settings.json",
      "key": "ROLLING_CONTEXT_UPSTREAM",
      "wrote": "http://127.0.0.1:8787",
      "displaced": null
    },
    {
      "project": "/home/dd/proj/A",
      "path": "/home/dd/proj/A/.claude/settings.local.json",
      "key": "ANTHROPIC_BASE_URL",
      "wrote": "http://127.0.0.1:5588",
      "displaced": "http://127.0.0.1:8787"
    }
  ],
  "alerted": [{"project": "/home/dd/proj/A", "url": "http://127.0.0.1:8787"}]
}
```

Two fields:

- **`writes`** — one entry per key we set, in the order we set it. `wrote` powers
  the read-back guard; `displaced` powers strict undo (D7). `"displaced": null`
  means the key was absent before us, so undo deletes it. `unchain` walks the list
  in reverse. Each entry also carries the `project` whose `chain` call created it,
  which is what makes the shared-key rule below expressible.

### The shared key, and why `writes` is append-only per project (D17)

`ANTHROPIC_BASE_URL` is written per project. `ROLLING_CONTEXT_UPSTREAM` is not:
it lives in one file, `~/.claude/settings.json`, shared by every chained project.
D10 permits projects A and B to chain concurrently to the same URL, so both
record a `writes` entry naming the identical `(path, key)` tuple. Nothing about
that is redundant — each entry records that one more project depends on the key
being set — but it means a `(path, key)` tuple no longer identifies one write.

Two rules follow, and `unchain` (§6) depends on both:

- **Append, don't dedupe.** Project B's `chain` records its own entry even when
  the key already holds exactly the value B would write. B's `wrote` is that
  value and B's `displaced` is what B found — which is A's value, i.e. our own.
  Skipping the record because "the key is already right" would lose the fact that
  B now depends on it.
- **The last reference restores; the others just drop their entry.** The
  reference count is *derived* from `writes` — the number of entries for that
  `(path, key)` tuple — not stored in a separate counter that could disagree with
  the list it describes. When more than one remains, `unchain` removes only its
  own project's entry and leaves the key alone. When its entry is the last one,
  it restores using the **earliest** recorded `displaced` for that tuple — the
  value that predates rolling-context entirely. Restoring B's `displaced` would
  write our own chained value back as though a user had configured it, leaving
  the machine chained after the last project unchained.

Without this, plain `unchain` in A deletes or rewrites the key out from under B.
B's `ANTHROPIC_BASE_URL` still points at us, so §8's displacement check stays
quiet; B's own recorded value was never touched, so §8's drift check stays quiet
too; and B's traffic silently falls from §7's tier 2 through to tier 3 or 4. That
is the exact silent-corruption class §3's Governing Rule and D7 exist to prevent.
- **`alerted`** — `{project, url}` pairs already announced (D8). Keyed on project,
  not just URL: `headroom wrap claude` binds the same port every time, so a
  URL-only key alerts the user exactly once in the lifetime of the install and
  goes silent in every project after the first — reproducing the defect this
  design exists to fix, one field over. A pair is "alerted" only once its own
  `{project, url}` tuple has been recorded.

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

- Lock scope: the state-file lock is held from before guard evaluation through
  both settings writes, both read-backs, and the state-file write — not narrowly
  around the `alerted` append. D10 explicitly permits concurrent `chain` calls
  from different projects; without this scope two concurrent applies can
  interleave their read-backs against each other's writes.
- POSIX: `fcntl.flock` on the state file, held for the scope above. Windows has
  no `fcntl`; the Windows path uses `msvcrt.locking` on the same file under the
  same critical section, selected by a top-of-module platform check so the
  `fcntl` import itself never executes on Windows — a module-level `import fcntl`
  on Windows breaks `is-self` (and everything that imports `chain.py`) on every
  invocation, including from `.ps1` callers.
- Atomic `tmp` + `os.replace`.
- **Unparseable JSON → refuse and report, never overwrite.** Review round 4 found a
  fallback to `{}` that destroyed an entire settings file. Applies to the state
  file and to every settings file we touch.
- Settings files are read, mutated in memory, and written back whole — never
  regenerated.
- The state file is written `0600`: it names project paths and the topology of a
  locally-chained proxy, and there's no reason another local user should read it.

## 6. Verbs

Shared resolution: walk scopes in the Fact 3 order and return the winning value
**and the file it came from**. Displaced = the winner is not ours.

### `is-self`

The single predicate all seven call sites (`server.py:93` plus the six shell/
PowerShell sites collapsed in §9) reduce to. Contract:

```
is_self(url) := parse(url).scheme in {http, https}
             and host_matches(parse(url).host, OUR_BIND_HOST)
             and port_matches(parse(url).port_or_default, OUR_BIND_PORT)
```

`OUR_BIND_HOST`/`OUR_BIND_PORT` come from the daemon's own actual bind address at
call time (never a hardcoded `127.0.0.1:5588`), so a non-default
`ROLLING_CONTEXT_PORT` still self-detects correctly. `host_matches` treats
`127.0.0.1`, `::1`, and `localhost` as equivalent; a bare host with no port uses
the scheme's default (80/443) before comparing. `same-port-different-host` (below)
is the guard for the case this predicate correctly does **not** catch: a foreign
proxy on our port but a different host is not us, and chaining to it is legal.

### `chain`

Guards, in order. Each refuses with a named reason, a fixed user-facing message,
and writes nothing; the two no-ops exit 0.

| Reason | Condition | Exit | Message |
|---|---|---|---|
| `not-displaced` | the winner is already ours | 0 | `already chained through you — nothing to do` |
| `nothing-to-chain` | the winner is the default API; no foreign proxy | 0 | `no foreign proxy detected — nothing to chain` |
| `non-loopback` | the foreign URL's host is not loopback (D13) | 2 | `refusing to chain to <url> — not a loopback address. rolling-context only chains to local proxies (127.0.0.1/::1/localhost); chaining elsewhere would forward your API key off-machine` |
| `managed-scope` | the foreign value lives in `managed-settings.json` — unwinnable | 2 | `<url> is set by managed-settings.json — an administrator policy, not something rolling-context can override` |
| `same-port-different-host` | the foreign URL uses our port on another host | 2 | `<url> uses our own port on a different host — refusing, this looks like a misconfiguration rather than a proxy to chain to` |
| `divergent-chain` | `ROLLING_CONTEXT_UPSTREAM` in `~/.claude/settings.json` is set to a different URL (D10) | 2 | `already chained to <existing>; <url> is a different proxy. run 'unchain' first if you want to switch` |
| `upstream-pinned-by-env` | `ROLLING_CONTEXT_UPSTREAM` is set in the process environment | 2 | `ROLLING_CONTEXT_UPSTREAM is set in your shell environment (<value>) — settings can't override that. unset it or edit your shell config instead` |
| `unparseable-settings` | a target settings file or the state file is invalid JSON | 2 | `<path> is not valid JSON — refusing to touch it. fix the file by hand and retry` |
| `declined` | the D14 scope-escalation confirmation was answered anything but `y`, or stdin is not a TTY and `--yes` was not passed | 2 | `not chained — confirmation declined. re-run with --yes to skip the prompt` |

`declined` is a guard like any other: nothing is written, the state file is
untouched, and the exit code is the same 2 every other refusal uses. It is
evaluated after the guards above it, since there is no point asking the user to
confirm a chain that would be refused anyway. Non-interactive with no `--yes` is a
decline rather than a hang: a hook or CI invocation that would block forever on a
prompt nobody can answer is worse than a named refusal.

`divergent-chain` compares against the settings value only, and allows the matching
case: a second project wrapped by the same proxy URL needs the same upstream, so it
is harmless and permitted.

`upstream-pinned-by-env` exists because tier 1 outranks tier 2 (section 7). With the
variable exported, writing it to settings would change nothing, so `chain` refuses
and names the variable rather than appearing to succeed. This is the surviving arm
of the previous design's D4.

Apply, **upstream first, base URL second**, under the state-file lock (D10's
concurrent-chain guarantee depends on this lock covering the whole sequence, not
just the final write — see §5 Writing rules):

1. Print the scope-escalation notice and get confirmation (D14) before doing
   anything else: `chain` is about to make `<url>` upstream for every project on
   this machine, via `~/.claude/settings.json`. Requires `y`, or `--yes` to skip
   the prompt; anything else refuses via the `declined` guard above.
2. Record both intended `writes` entries in the state file.
3. Write `ROLLING_CONTEXT_UPSTREAM` = the foreign URL to `~/.claude/settings.json`.
4. Write `ANTHROPIC_BASE_URL` = `http://127.0.0.1:$ROLLING_CONTEXT_PORT` to the
   file that displaced us.
5. Read both back. On mismatch, undo in reverse and report failure.

The order of steps 3-4 is not arbitrary. Reversing it would point Claude Code at us
before we know where to forward, and "no upstream recorded" resolves to the default
API — silently un-chaining the user, which D9 forbids.

### `unchain`

Scope: entries whose `path` lies inside the **project root** — the nearest
ancestor of the current directory, stopping strictly before `$HOME`, that
contains a `.claude` directory — plus the `~/.claude` upstream entry that belongs
to them. If no such ancestor exists between the current directory and `$HOME`
(exclusive), there is no project-scoped entry to unchain; report that and exit 0.
This exclusion is load-bearing: `$HOME/.claude` always exists, so a walk that
doesn't stop before it would treat the user's home directory as "the project" for
any cwd lacking its own `.claude`, silently widening every plain `unchain` call to
`--all`'s scope. Uninstall passes `--all` explicitly, which skips this walk
entirely and matches every recorded entry.

Per entry, in reverse order, **read back before writing**: if the current value
equals our `wrote`, restore `displaced` byte-exact, or delete the key when
`displaced` is null. If it differs — the other proxy's exit already removed it, or
the user edited it — skip, report, and leave the file alone. Drop the entry either
way.

**The shared upstream key is the exception** (D17, §5). Before touching
`(~/.claude/settings.json, ROLLING_CONTEXT_UPSTREAM)`, count the remaining
`writes` entries for that tuple. If entries from other projects remain, drop only
this project's entry and leave the key set — those projects are still chained
through it. If this is the last entry, restore the **earliest** recorded
`displaced` for the tuple rather than this entry's own, which is only ever our own
value written back by a later project's `chain`. `--all` removes every entry, so
the count reaches zero in one pass and the earliest `displaced` is restored once.

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
upstream: https://api.anthropic.com (default)
compaction: OFF this session
fix: /rolling-context:chain
```

The `upstream` line reports §8's drift dimension too, so drift is inspectable on
demand and not only at `SessionStart`: when the live `ROLLING_CONTEXT_UPSTREAM`
differs from what `chain` recorded, the line names both values and marks it
`(changed outside chain)`.

Guard messages here name the bare verb (`run 'unchain' first`) because a user is
reading them in a terminal where `chain.sh` is already the running command. §7's
dead-upstream message deliberately spells out the full
`bash <resolved>/hooks/chain.sh unchain` form instead, because its reader is the
model, mid-session, with no shell context and no working API to ask about one. The
inconsistency is deliberate; don't "fix" it.

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

**Loopback enforcement at resolution time (D18).** Tiers 2 and 3 — both
file-sourced — additionally require a loopback host. A non-loopback value from a
settings file is refused, not forwarded to: resolution reports the reason and the
request gets the dead-upstream error shape below, naming the offending file. This
is defense in depth for the path `chain`'s D13 guard cannot cover, since `chain`
is not the only thing that can write those files. Tier 1 is exempt by design: an
exported `ROLLING_CONTEXT_UPSTREAM` is the user acting deliberately in their own
shell, and the refusal would break a legitimate remote-gateway setup with no way
to opt back in.

**Accessor shape (D15).** The accessor returns `Upstream(scheme, host, port,
path)`, parsed once per resolution and cached (below), not a plain string. This is
the fix for the actual bug: `server.py:100`'s single `UPSTREAM_URL` assignment is
not the only frozen value downstream of it — `_parsed_upstream` (`:123`) and
`UPSTREAM_PATH` (`:124`) are separately derived at import time and read again at
`:634`, `:767`, `:865`, `:869`, `:1065`, and the connection factory at `:151-161`
builds its socket directly from `_parsed_upstream.scheme/.hostname/.port`. A
string-only accessor leaves every one of those frozen. Six literal `UPSTREAM_URL`
consumers plus these three derived-value sites all become reads of the one
`Upstream` struct.

**Caching:** `stat()` `~/.claude/settings.json` per request; re-read only when
`mtime_ns` or size change. Atomic `os.replace` makes the stat a reliable trigger.
The parsed `Upstream` struct is cached alongside the raw string and invalidated on
the same trigger, so callers never re-parse per request.

**Loop protection (D16).** Every forwarded request carries
`X-Rolling-Context-Chained-From: <our scheme>://<our host>:<our port>`, derived
from the daemon's own live bind address rather than a hardcoded value, and always
written in the loopback form even if the bind host is ever made configurable. An
inbound request that already carries this header naming *this* daemon's own
address is refused with a loop-detected error rather than forwarded — `is_self`
only catches chaining directly to ourselves; this catches a longer cycle formed
through an intermediate proxy that (mis)configures its own upstream back at us.
The comparison runs through the same `host_matches`/`port_matches` normalization
as `is-self` (§6), so an intermediate that relays `localhost` against our
`127.0.0.1` is still caught; a raw string compare would not catch it.

The header is trusted input from a local process, which is the same trust
boundary the listener already assumes — anything that can reach `:5588` can
already send requests as us. A local process that forges the header can therefore
force a false loop-refusal of a legitimate session. That is a denial of
availability, not of confidentiality, by a process that could equally just kill
the daemon; it is accepted as residual rather than mitigated, and noted here so
the acceptance is explicit.

**Dead upstream (D9).** Connection-level failure — refused, DNS failure,
unreachable — returns an Anthropic-shaped error body, so Claude Code renders it as
a message rather than a transport crash:

```json
{"type":"error","error":{"type":"api_error","message":
 "rolling-context: chained upstream http://127.0.0.1:8787 is not answering. Run: bash <resolved>/hooks/chain.sh unchain"}}
```

`<resolved>` is computed from the running server's own location, never hardcoded —
the plugin may be a symlink at `$HOME/.claude/plugins/rolling-context` or a
checkout elsewhere. The upstream URL embedded in this message is the parsed
`Upstream` struct re-serialized from its validated fields, never the raw string
interpolated verbatim — the same rule applies everywhere a chained URL reaches a
message or a file (state file, `/health`, this error), closing the path from an
attacker-controlled or malformed URL string to injected text in output a user or
model reads.

The message names the **shell** form, not the slash command: the model cannot
answer while its own requests are failing, so the escape hatch must not require a
model round-trip. When the upstream came from tier 1, the message names the
environment variable instead. HTTP statuses returned by a live upstream pass
through untouched; only failures to reach it produce this.

**Two consumers that must follow the upstream or fail silently:**

- **The summarizer.** `compressor.py:38-40` derives `SUMMARIZER_BASE_URL` from
  `ROLLING_CONTEXT_UPSTREAM` at import, `:56-60` freezes host, port, scheme and
  path, and `:445`, `:532`, `:583`, `:593` build request paths from the frozen
  value (plus log lines at `:534`, `:611`). With a per-request upstream, a frozen
  summarizer URL sends compaction traffic to the wrong place — the feature being
  restored, failing quietly. It resolves at call time via the same `Upstream`
  accessor, unless `ROLLING_CONTEXT_SUMMARIZER_URL` is set, in which case that
  override stays authoritative (`SUMMARIZER_URL_SET`).
- **The `UPSTREAM_URL` string and derived-value consumers** in `server.py`
  (enumerated above) become calls to the one `Upstream` accessor.

**Response header logging.** Request-side header logging (`:446`, `:794`) is
already name-only/filtered; response-side (`:642`, `:877`) currently logs header
*values* unfiltered at DEBUG. Same filter applies to both — a chained upstream is
attacker-influenceable in a way `api.anthropic.com` is not, so this stops being a
theoretical gap once chaining ships.

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

Keyed per `{project, url}` (§5) — not URL alone. Headroom binds a fixed port, so a
URL-only key would alert once across the entire install's lifetime, the original
bug in a new place: project B's first encounter with the same displacing proxy
would be silently suppressed by project A's earlier alert.

| Situation | Alert? |
|---|---|
| foreign URL not in `alerted` for this project | yes, then record it |
| foreign URL in `alerted` for this project, no write recorded for that file | no |
| foreign URL in `alerted` for this project, **a write is recorded for that file** | **yes** — our chain was displaced |

The third row uses different wording ("your chain was overwritten") and does not
depend on which tool did it. Silence there would recreate the original bug in a new
place.

There is no "everything is fine" line. Silence plus `status` is the contract.

### Upstream-key drift (distinct from displacement)

Displacement is `ANTHROPIC_BASE_URL` no longer pointing at us. A second, separate
failure mode is `ANTHROPIC_BASE_URL` still pointing at us while
`ROLLING_CONTEXT_UPSTREAM` — the value `chain` wrote — has itself changed or gone
missing underneath us: another tool rewrites `~/.claude/settings.json` wholesale
and drops or overwrites the key, or a hand-edit clobbers it. Nothing in the
displacement check above would catch this, because from `ANTHROPIC_BASE_URL`'s
point of view nothing changed — we are still in the request path — but we are now
silently chaining somewhere the user did not choose, or silently un-chained.

Detected the same way as displacement: `SessionStart` compares the live
`ROLLING_CONTEXT_UPSTREAM` value against what `chain`'s write recorded (§5) for
this project. Mismatch fires the same two-field contract:

```json
{"hookSpecificOutput":{"hookEventName":"SessionStart",
  "additionalContext":"rolling-context's chain target changed outside of /rolling-context:chain — was http://127.0.0.1:8787, is now <current>. Verify this is intended, or re-run /rolling-context:chain."},
 "systemMessage":"[rolling-context] chain target changed outside chain.sh: was http://127.0.0.1:8787, now <current>.
  check: /rolling-context:status     re-chain: /rolling-context:chain <url>"}
```

Suppressed by the same per-project `alerted` record, keyed on the pair (old
recorded value, new observed value) so a second unrelated drift after the first is
still surfaced.

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
`uninstall.ps1:101`, `server.py:93` — collapse to one implementation, whose
contract is defined in §6 (`is-self`). Shell calls `python3 chain.py is-self
<url>`; PowerShell calls `python chain.py is-self <url>`, matching the existing
convention at `start-proxy.ps1:101`, `install.ps1:18` and `install.ps1:102` —
`python3` is frequently absent from `PATH` on Windows, so a `python3` invocation
from a `.ps1` would fail on exactly the platform it runs on.

`grep '127\.0\.0\.1'` **undercounts these sites**: the `.ps1` files store escaped
dots (`127\.0\.0\.1` inside a `-notmatch` regex), which is how a site was missed
before. Legitimate non-predicate literals, which the migration must leave alone:
`start-proxy.sh:11`, `install.sh:11`, `start-proxy.ps1:13`, `install.ps1:27`,
`server.py:1068` (the listener's own bind address).

## 10. Verification

Every item RED first, and each new test checked against the old code or a mutant to
prove it can fail. Review round 5's finding 1 was "free to happen" precisely
because no test covered state-file version handling at all.

| Test file | What must fail first |
|---|---|
| `test_is_self.py` | the single predicate across all four semantics the seven sites had, including `:8787`-is-not-us |
| `test_effective_value.py` | Fact 3 precedence, and that the source *file* is reported |
| `test_chain_verb.py` | each refusal reason including `declined` and `non-loopback`; the two exit-0 no-ops; upstream-before-base-URL order; reverse undo when read-back fails; the D10 matching-URL success path — a second project chaining to the already-recorded URL writes successfully and appends its own entry |
| `test_chain_confirm.py` | the D14 gate: a declined answer writes nothing and exits 2; `--yes` bypasses the prompt; non-interactive stdin without `--yes` refuses rather than hanging; the notice names both the destination and the machine-wide scope |
| `test_unchain_verb.py` | restore vs delete on `displaced: null`; skip when our value is gone; reverse order; project-root scoping; `--all` |
| `test_unchain_shared_key.py` | D17: with two projects' entries present, project A's `unchain` drops only its own entry and leaves `ROLLING_CONTEXT_UPSTREAM` set, so project B still resolves at tier 2; the last remaining entry restores the **earliest** recorded `displaced`, not its own; `--all` reaches zero references in one pass |
| `test_loop_protection.py` | D16: a request carrying our own address in `X-Rolling-Context-Chained-From` is refused as a loop; a genuinely different chained-from address forwards normally; an alternate loopback spelling (`localhost` vs `127.0.0.1`) is still caught; the emitted value tracks the live bind address rather than a constant |
| `test_state_io.py` | atomic replace; refusal on unparseable state; flock under contention; the file is created mode `0600`, on both the create and the `os.replace` rewrite path |
| `test_server_upstream.py` | all four tiers, each `is_self`-guarded; cache invalidation on `mtime_ns` change; D18 — a non-loopback value at tier 2 or 3 is refused and names the offending file, while the same value at tier 1 is honoured |
| `test_response_header_logging.py` | response-side header logging at `server.py:642` and `:877` is name-only at DEBUG, matching the request-side filter at `:446`/`:794`; a chained upstream's header *values* never reach the log |
| `test_dead_upstream.py` | connection refused → Anthropic-shaped body; per-tier wording; the resolved path is the server's own; live upstream statuses pass through |
| `test_summarizer_follows.py` | the summarizer tracks a changed upstream; the explicit override still wins |
| `test_hook_output.py` | stdout is exactly one JSON object or empty; `systemMessage` present when displaced; the three-row suppression matrix keyed per `{project, url}` — including two projects, same displacing URL, both alerted; diagnostics never on stdout |
| `test_upstream_drift.py` | live `ROLLING_CONTEXT_UPSTREAM` differing from the recorded value fires the drift alert while `ANTHROPIC_BASE_URL` still points at us; the key going missing fires it; a second, different drift after the first is not suppressed |
| `test_status_verb.py` | reports chained/not-chained, the effective upstream and its source file, and reachability; exit codes distinguish healthy from displaced; it reads `/health` rather than re-deriving |
| `test_health_chain_fields.py` | `/health` exposes `chained` and `upstream_reachable` beside a sanitized `upstream_url` and `upstream_source`; the URL is re-serialized from the parsed struct, never echoed raw |
| `test_install_seeding.py` | the three-case seeding table (absent / ours / foreign) in `install.sh` and `start-proxy.sh`, with the foreign case writing nothing and printing guidance |
| `test_state_version.py` | an unknown/newer `version` in the state file is refused rather than silently coerced; a missing `version` is refused; the current version round-trips |
| `test_uninstall.py` | `unchain --all` before directory removal, **and** the reverse order, so the dependency stays explicit; skips reported; state file and lock removed |

Port from the old branch: `test_chain_write.py:231`
(`test_timeout_is_a_logged_noop_not_a_block`, on `feat/upstream-chaining`) kills a
`threading.Lock` mutant that the threaded barrier test is blind to (0/15). It must
not be weakened.

**End-to-end, unmocked**, by the method that produced Facts 1 and 2 — in-process
local listeners started by the test itself, no API contact and no container: a
chained request must arrive at the foreign listener with compaction applied, and a
dead upstream must yield the guidance error. Explicitly **not** via
`docker-compose.e2e.yml`, which sets `ROLLING_CONTEXT_UPSTREAM=https://api.anthropic.com`
in the environment — tier 1 of §7, which the `upstream-pinned-by-env` guard in §6
refuses to chain over. Running the chain E2E in that harness would test the refusal
path, not the chain. The in-process listeners the Fact 1/Fact 3 spikes already used
are the model.

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
| Alert on every displaced session | rejected in favour of once per `{project, url}` plus `status` (D8) |
| `chain` to a non-loopback URL behind an opt-out flag | the flag's only effect would be to forward the user's API key to an off-machine host (`server.py:437-443`); no in-scope use case needs it, and a flag that exists is a flag that gets pasted from a forum (D13) |
| Fall back to the API when the chained upstream is dead | routes traffic the user did not ask for (D9) |
| Newest chain wins across divergent projects | one daemon has one upstream and a request carries no project identity; taking over would silently mis-route the other project |
| Per-project concurrent upstreams | requires per-request routing the single daemon cannot provide |
| Patch `headroom wrap`, or use `ANTHROPIC_TARGET_API_URL` to put headroom outside | out of scope (R5) |
| Launcher script or documented config workaround | not a shipped feature (R5) |
| Loopback enforcement at all four resolution tiers, including the process environment | tier 1 is the user exporting a variable in their own shell — a deliberate act, not a file another tool can rewrite behind their back. Refusing it would break a remote-gateway setup with no opt-in left, so the enforcement stops at the file-sourced tiers (D18) |
| Storing an explicit reference count for the shared upstream key | a counter can disagree with the `writes` list it describes; the count is derivable from the list, so deriving it removes the failure mode instead of guarding it (D17) |
| A `--home` CLI flag for test convenience | production surface added for tests; tests set and restore `HOME` |

## 12. Open items

One, to be resolved by probe during implementation rather than assumed:

**Tier-1-over-tier-2 precedence is asserted, not measured.** §7 states that
`ROLLING_CONTEXT_UPSTREAM` from the process environment beats the value in
`~/.claude/settings.json`. That is how our own resolver will be written, so within
our code it is true by construction — but the claim that matters is the one about
*Claude Code's* behaviour when both are present, and no probe in §2 measured it.
Fact 3 measured project-`settings.local.json` versus inherited process env for
`ANTHROPIC_BASE_URL`, which is a different pair of sources and a different key.
Before implementing the `upstream-pinned-by-env` guard (§6), run a probe in the
style of the Fact 1/Fact 3 spikes — set both, observe which one a live session
actually uses — and record the result here as a measured fact. If the measurement
contradicts the assumption, the guard's refusal message and the tier order both
change.

Next step after that item is recorded: `writing-plans`.
