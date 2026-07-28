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
| D17 | `ROLLING_CONTEXT_UPSTREAM` lives in one file shared by every chained project, so it is not a `writes` entry at all: it gets its own `shared_upstream` object holding the pre-rolling-context `original` (captured once, never rewritten) and `refs`, the list of projects currently chained through it. The last project to leave restores `original`, under the usual read-back guard, and the object is then removed. `refs` entries naming directories that no longer exist are pruned by every verb, since nothing else could ever remove them and one would pin the key set forever. Without this, plain `unchain` in one project silently un-chains every other project chained to the same proxy, with no alert firing — and any scheme that derives the original from per-project records fails outright when projects unchain out of order, because the record holding the original is destroyed first. See §5. |
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
the Security and Architect reviewers in iterations 1 and 2 respectively, D17 in
the `shared_upstream` form after the Architect showed in iteration 3 that its
first draft was order-dependent; D18 answers the Security reviewer's iteration-2
question, decided by the user.

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
      "path": "/home/dd/proj/A/.claude/settings.local.json",
      "key": "ANTHROPIC_BASE_URL",
      "wrote": "http://127.0.0.1:5588",
      "displaced": "http://127.0.0.1:8787"
    }
  ],
  "shared_upstream": {
    "path": "/home/dd/.claude/settings.json",
    "key": "ROLLING_CONTEXT_UPSTREAM",
    "wrote": "http://127.0.0.1:8787",
    "original": null,
    "refs": ["/home/dd/proj/A"]
  },
  "alerted": [{"project": "/home/dd/proj/A", "url": "http://127.0.0.1:8787"}]
}
```

Three fields:

- **`writes`** — one entry per **project-scoped** key we set, in the order we set
  it. `wrote` powers the read-back guard; `displaced` powers strict undo (D7).
  `"displaced": null` means the key was absent before us, so undo deletes it.
  `unchain` walks the list in reverse. In practice this is `ANTHROPIC_BASE_URL`
  in a project's own settings file, and `project` records whose `chain` call
  created it.
- **`shared_upstream`** — the one machine-wide key, held separately. See below.
- **`alerted`** — `{project, url}` pairs already announced (D8). Keyed on project,
  not just URL: `headroom wrap claude` binds the same port every time, so a
  URL-only key alerts the user exactly once in the lifetime of the install and
  goes silent in every project after the first — reproducing the defect this
  design exists to fix, one field over. A pair is "alerted" only once its own
  `{project, url}` tuple has been recorded.

### The shared key is not a `writes` entry (D17)

`ANTHROPIC_BASE_URL` is written per project. `ROLLING_CONTEXT_UPSTREAM` is not:
it lives in one file, `~/.claude/settings.json`, shared by every chained project.
D10 permits projects A and B to chain concurrently to the same URL, so if this key
were recorded in `writes` two entries would name the identical `(path, key)`
tuple, and the tuple would no longer identify one write. It is therefore held in
its own object, not in `writes` at all:

```json
"shared_upstream": {
  "path": "/home/dd/.claude/settings.json",
  "key": "ROLLING_CONTEXT_UPSTREAM",
  "wrote": "http://127.0.0.1:8787",
  "original": null,
  "refs": ["/home/dd/proj/A", "/home/dd/proj/B"]
}
```

- **`original` is captured once and never rewritten.** It is whatever the key held
  the first time any project chained — the value that predates rolling-context
  entirely, `null` if the key was absent. When a second project chains, `original`
  is left exactly as it is; what B "displaced" is our own value, which is not a
  thing anyone should ever be restored to.
- **`refs` is the reference count, as a list of projects rather than a number.**
  `chain` appends the project, `unchain` removes it. A list cannot drift out of
  agreement with itself the way a count maintained beside a separate list can, and
  it also answers *which* projects are still chained, which the `unchain` message
  and `status` both need.
- **Restore happens when `refs` empties, and only then**, back to `original`,
  under the same read-back guard as every other write: if the live value no longer
  equals `wrote`, skip and report rather than overwrite. §8's drift case is
  precisely a live value changing underneath us, so the last unchain must not
  assume it still owns the key.
- **The object is removed once `refs` empties** — in both outcomes. If the restore
  happened, there is nothing left to track. If the read-back skipped it, we have
  just established that we no longer own the key, and retaining an `original` we
  will never write back is dead state that a later `chain` would have to reason
  about. `chain` therefore captures `original` if and only if no `shared_upstream`
  object exists; an object with an empty `refs` array is not a state this file ever
  holds.

**Entries for projects that are gone are pruned.** `refs` names project
directories, and one can vanish without its project ever running `unchain`.
Nothing else could remove that entry — `unchain` finds the current project by
walking up from cwd (§6), and there is no cwd inside a directory that no longer
exists — so the entry would pin `refs` non-empty forever and the key would never
be restored, leaving the machine chained to a proxy no live project depends on.

**The prune condition is the absence of the project's `.claude` directory**, not
of the project root. §6 defines a project root as the nearest ancestor containing
`.claude`, so `.claude` is what makes a directory a project at all. Testing the
root alone would miss the case where the checkout survives but its `.claude` was
deleted — a reset, a move out of a monorepo, a `rm -rf .claude` — leaving
`isdir(project)` true, the entry unpruned, and the key pinned exactly as before. A
deleted project root takes its `.claude` with it, so the single condition covers
both.

**Only `chain` and `unchain` prune** — both already hold the lock across their
whole sequence and are already writers. `status` never mutates state (§6): it
reports stale entries and names the remedy. An observability command a user is
told to run anytime should not rewrite shared state as a side effect, and making
it a writer would serialize every routine check behind any in-flight `chain` or
`unchain`.

**A verb never prunes the entry it is itself about to write or remove.** The prune
considers only other projects' entries, so a `chain` re-run from a project whose
recorded path spelling differs cannot delete its own reference out from under
itself.

**Recorded paths are sanitized before they are shown, exactly as URLs are.** A
project directory name is chosen by whoever created it — any bytes but NUL and
`/`, including terminal escape sequences and text written to read as an
instruction — and it arrives on the machine through an ordinary `git clone`. It is
therefore no more trusted than a chained URL. Per D6 these verbs also ship as
slash commands, so `status` and `unchain` output is not only a human reading a
terminal: it is tool output the **model** reads in the same conversation. A
`project` string is never interpolated raw into a message, a log line, or the
state file; control and non-printable bytes are escaped first, by the same rule
§7 applies to URLs. What this buys is structural safety — no terminal escape
sequences, no injected newlines, no corrupted JSON. It does not neutralize a
directory whose name is plain printable text shaped like an instruction: there is
nothing there to escape, and it reaches the model as written. That residue is the
same one §7 already accepts for URL path components, and it is named here so no
one mistakes escaping for an injection defense. The earlier note in §6 about guard messages being read by a
person in a terminal describes only their tone, never their escaping.

**Write targets must stay inside the project.** `chain` writes to
`<project>/.claude/settings.local.json`. Canonicalizing the project root does not
constrain that path, since a clone can ship `.claude` as a symlink pointing
anywhere; the resolved write target is therefore required to lie inside the
resolved project root, and `chain` refuses with a named reason when it does not.
The write step consumes the path the guard already resolved rather than
re-resolving `<project>/.claude/settings.local.json` at write time, so there is no
second resolution that could disagree with the one that was validated.

**Recorded paths are canonical.** Every `project` string is `realpath`'d before it
is stored and before any comparison — removal, prune, and the `refs` membership
test alike. Reached through a symlink, or spelled relatively on one invocation and
absolutely on the next, the same project would otherwise fail to match its own
entry: `unchain` would silently leave a live reference behind, and the prune would
skip a stale one, neither hitting an error path.

The prune misfires whenever a project's `.claude` is absent at check time while
the project is still live — a temporarily unmounted volume, or any local process
deleting `.claude` between one verb and the next. It is pruned, and if it was the
last reference the key is restored while that project still believes itself
chained. That failure is visible rather than silent — the
project's `ANTHROPIC_BASE_URL` still points at us while `ROLLING_CONTEXT_UPSTREAM`
no longer matches `shared_upstream.wrote`, which is exactly §8's drift alert — and
the fix is one `chain` away. A pinned key that never restores has no such
self-announcing failure, which is why the prune is the safer default.

Because `status` does not prune, a machine whose *only* chained project was deleted
keeps the key set until someone runs `chain` or `unchain` again. That state is
louder than it first sounds, and deliberately so: `ROLLING_CONTEXT_UPSTREAM` is
machine-wide, so once the pin is stale **every** project routed through the daemon
gets D9's dead-upstream error — including projects that never ran `chain` and have
no entry in `refs`. The failure announces itself to everyone sharing the daemon
rather than degrading one project quietly.

It also heals from anywhere. `unchain` run in such an unrelated project still
reaches the shared-key handling — it has a `.claude` directory, so the no-ancestor
early exit does not catch it — and the prune-before-branch ordering clears the
stale entry as a side effect, even though that project was never a reference. So
the remedy is not "find the deleted project," which is impossible; it is any
`chain` or `unchain` anywhere on the machine, which `status` names.

This ordering-independence is the whole point. An earlier draft of D17 kept the
key in `writes`, one entry per project, and had the last remaining entry restore
the *earliest* recorded `displaced`. That is correct only if projects unchain in
the reverse of the order they chained. Take A chaining over a pre-existing `X0`,
then B chaining to the same URL, then **A unchaining first**: A is not the last
reference, so its entry — the only record of `X0` — is dropped, and when B later
unchains as the last reference the earliest `displaced` still present is B's own,
which is our chained value. The machine is left chained to the foreign proxy after
the last project unchained. Deriving the original from records that are destroyed
on the way out cannot work; the original has to be stored once, outside them.

Without any of this, plain `unchain` in A deletes or rewrites the key out from
under B. B's `ANTHROPIC_BASE_URL` still points at us, so §8's displacement check
stays quiet; B's own recorded value was never touched, so §8's drift check stays
quiet too; and B's traffic silently falls from §7's tier 2 through to tier 3 or 4.
That is the exact silent-corruption class §3's Governing Rule and D7 exist to
prevent.

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
  around the `alerted` append.
  This means `chain`'s confirmation prompt (D14) is inside the lock, so a session
  left sitting at the `y/N` prompt blocks a concurrent `chain` or `unchain` in
  another project until someone answers. Accepted: it cannot hang a script, since
  a non-interactive caller without `--yes` refuses immediately rather than
  prompting, and the alternative — confirm outside the lock, then acquire it and
  re-evaluate every guard before writing — buys freedom from human latency at the
  cost of a second guard pass and two places where guard results can disagree. Not
  worth it for a prompt a person is looking at. The block is also scoped to the
  same principal: it is the user's own other invocation that waits, on a machine
  whose trust boundary is already a single local user (D13, D18).
- `chain`'s prune step can restore `original` to `~/.claude/settings.json` and
  then overwrite it with the new upstream two steps later — two `os.replace`
  calls on that file inside one lock hold. The flock keeps *our* read-backs
  consistent but does not make the pair atomic to an unrelated reader, so the
  file transiently holds the pre-chain value. Named here rather than left
  implicit, in keeping with the rest of this section. D10 explicitly permits concurrent `chain` calls
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
proxy on our port but a different host is genuinely not us, so `is-self` must
return false — and the separate guard then refuses to chain to it, because a
foreign proxy squatting our own port on another host is far more likely a
misconfiguration than a deliberate topology. `is-self` classifies; the guard
decides. They are not the same question, and only the guard is about legality.

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
| `write-target-escapes-project` | `<project>/.claude/settings.local.json` resolves outside the project root, e.g. `.claude` is a symlink | 2 | `<project>/.claude does not resolve inside <project> — refusing to write through it` |
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
2. Prune `refs` of any *other* project whose `.claude` directory is gone (§5),
   reporting each drop. This runs here, on the success path inside the lock — not
   before guard evaluation — so the guarantee that a refused `chain` writes nothing
   holds with no exception carved into it.
3. Record the intended `ANTHROPIC_BASE_URL` write in `writes`, and record this
   project in `shared_upstream.refs` — capturing `original` from the key's current
   value if and only if no `shared_upstream` object exists yet (§5), so the first chain on the machine is the
   one that snapshots what predates us (D17, §5).
4. Write `ROLLING_CONTEXT_UPSTREAM` = the foreign URL to `~/.claude/settings.json`.
5. Write `ANTHROPIC_BASE_URL` = `http://127.0.0.1:$ROLLING_CONTEXT_PORT` to the
   file that displaced us.
6. Read both back. On mismatch, undo in reverse and report failure.

The order of steps 4-5 is not arbitrary. Reversing it would point Claude Code at us
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

**The shared upstream key is handled separately** (D17, §5), because it is not in
`writes`. Remove this project from `shared_upstream.refs`, then:

- **`refs` still non-empty** — leave the key exactly as it is; those projects are
  still chained through it. Say so rather than exiting silently, since the user
  just ran a command whose name promises removal:
  `left ROLLING_CONTEXT_UPSTREAM set — still chained by: <project>[, <project>…]`.
- **`refs` now empty** — restore `original` byte-exact, or delete the key when
  `original` is null. **Read back first**, exactly as for every entry in `writes`:
  if the live value no longer equals `shared_upstream.wrote`, something else
  changed it — §8's drift case is precisely this — so skip, report, and leave the
  file alone. The last unchain does not get to assume it still owns the key.

Before either branch, and under the same flock that covers the rest of `unchain`'s
sequence, prune `refs` of any other project whose `.claude` directory is gone (§5),
reporting each drop — this is the only thing that can remove such an entry,
and it is why `--all` is not the recovery path for a deleted project. `--all`
remains what uninstall invokes: it force-removes every reference including live
ones, which is correct when the plugin is going away and wrong as a routine remedy.

`--all` empties `refs` in a single pass, so it takes the restore branch once, with
the same read-back. It is the same branch and the same guard as the sequential
case, not a separate path with different safety properties — which is only true
because `original` lives outside the per-project records and cannot be destroyed
by whichever project happens to leave first.

This guard is also what prevents resurrecting a dead port: headroom deletes that
key when it exits (`wrap.py:1779-1781`), removing our value first, so `unchain`
correctly finds nothing to undo.

### `status`

The counterweight to D8's silence. Prints: our port and whether the daemon answers
`/health`; the effective base URL and **which file supplies it**; whether we are in
the path; the recorded upstream and whether it is reachable; recorded writes;
alerted URLs; and any `refs` entry whose project is gone.

**`status` writes nothing.** It takes no lock, mutates no settings file, and does
not prune — it *reports* stale entries and names the remedy. A command a user is
told to run anytime should not rewrite shared state as a side effect of being run,
and making it a writer would serialize every routine check behind any in-flight
`chain` or `unchain`. Pruning belongs to the two verbs that already hold the lock
and are already writing (§5).

```
rolling-context: daemon up on :5588
in path:  no  -- :8787 wins, from /home/dd/proj/A/.claude/settings.local.json
chained:  no
upstream: https://api.anthropic.com (default)
compaction: OFF this session
fix: /rolling-context:chain
```

When a recorded project is gone, it says so and does not act:

```
stale:    /home/dd/proj/A is recorded as chained but no longer exists
          ROLLING_CONTEXT_UPSTREAM stays set until the next chain or unchain clears it
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
model round-trip. `unchain` is the wrong advice when the upstream came from tier
1, though — the variable is in the user's shell and no file write can override it
— so that case substitutes its own message, again literal:

```json
{"type":"error","error":{"type":"api_error","message":
 "rolling-context: chained upstream http://127.0.0.1:8787 is not answering. It comes from ROLLING_CONTEXT_UPSTREAM in this session's environment, so unchain cannot clear it — unset ROLLING_CONTEXT_UPSTREAM in your shell and restart the proxy."}}
```

The D18 refusal reuses the same shape with the reason and the offending file
named: `rolling-context: refusing to use chained upstream https://proxy.example.com
from /home/dd/.claude/settings.json — it is not a loopback address, and forwarding
there would send your API key off-machine. Fix or remove that value.` HTTP statuses returned by a live upstream pass
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

The third row does not depend on which tool did it, and gets its own text — this
user already chained deliberately, so "another proxy holds the variable" would
understate what happened:

```json
{"hookSpecificOutput":{"hookEventName":"SessionStart",
  "additionalContext":"rolling-context was chained through http://127.0.0.1:8787 in this project, but something has since overwritten ANTHROPIC_BASE_URL and rolling-context is out of the request path again; context compaction is not running. Re-running /rolling-context:chain restores it."},
 "systemMessage":"[rolling-context] your chain was overwritten — compaction is OFF this session. http://127.0.0.1:8787 holds ANTHROPIC_BASE_URL again.\n  fix: /rolling-context:chain     check anytime: /rolling-context:status"}
```

Silence there would recreate the original bug in a new place.

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
`ROLLING_CONTEXT_UPSTREAM` value against `shared_upstream.wrote` (§5). Mismatch fires the same two-field contract:

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
| `test_is_self.py` | the single predicate across all four semantics the seven sites had, including `:8787`-is-not-us; a non-default `ROLLING_CONTEXT_PORT` still self-detects, since the predicate reads the live bind address rather than a hardcoded `5588`; same-port-different-host is **not** self (the guard, not the predicate, refuses it) |
| `test_effective_value.py` | Fact 3 precedence, and that the source *file* is reported |
| `test_chain_verb.py` | pruning of another project's dead `refs` entry on the success path, and **not** on any guard-refused path, so "guards write nothing" holds; each refusal reason including `declined` and `non-loopback`; the two exit-0 no-ops; upstream-before-base-URL order; reverse undo when read-back fails; the D10 matching-URL success path — a second project chaining to the already-recorded URL writes successfully and appends its own entry |
| `test_chain_confirm.py` | the D14 gate: a declined answer writes nothing and exits 2; `--yes` bypasses the prompt; non-interactive stdin without `--yes` refuses rather than hanging; the notice names both the destination and the machine-wide scope |
| `test_unchain_verb.py` | restore vs delete on `displaced: null`; skip when our value is gone; reverse order; project-root scoping, explicitly including the no-ancestor-before-`$HOME` case that must report and exit 0 rather than treating `$HOME/.claude` as the project; `--all` |
| `test_unchain_shared_key.py` | D17: with A and B both in `shared_upstream.refs`, A's `unchain` removes only A and leaves the key set, so B still resolves at tier 2, and the message names B as still chained; the last project out restores `original`; **out-of-order unchain** (A chains over a pre-existing value, B chains to the same URL, A unchains first, then B) still restores that pre-existing value, which is the case the previous derive-from-`writes` design got wrong; a drifted live value fails the read-back and is left alone rather than overwritten; `--all` empties `refs` in one pass and takes the same guarded restore branch; a `refs` entry whose project is gone is pruned by `chain` and `unchain`, and when it was the last reference the key is restored rather than pinned forever; the prune fires on a project whose `.claude` was deleted while the project root survives, not only on a deleted root; a verb never prunes its own project's entry; paths are `realpath`'d before storage and before every comparison, so a symlinked or relatively-spelled project still matches its own entry; the `shared_upstream` object is gone from the state file after `refs` empties, on both the restored and the read-back-skipped path |
| `test_loop_protection.py` | D16: a request carrying our own address in `X-Rolling-Context-Chained-From` is refused as a loop; a genuinely different chained-from address forwards normally; an alternate loopback spelling (`localhost` vs `127.0.0.1`) is still caught; the emitted value tracks the live bind address rather than a constant |
| `test_path_sanitizing.py` | a project directory whose name carries terminal escape sequences, newlines, or other control bytes reaches `status` output, `unchain` output, the log and the state file with those bytes escaped — the same rule `test_dead_upstream.py` pins for URLs. This is a **structural** guarantee: no terminal control, no broken log lines, no corrupted JSON. It is not an anti-prompt-injection measure and must not be tested as one — a directory named in plain printable English has no bytes to escape and reaches the model intact, exactly as §7 already accepts for URL path components. Also: a `.claude` symlink pointing outside the project root is refused by `chain` with the named reason rather than written through |
| `test_state_io.py` | atomic replace; refusal on unparseable state; flock under contention; the file is created mode `0600`, on both the create and the `os.replace` rewrite path; a chained URL written to the state file is re-serialized from the parsed `Upstream`, never the raw string |
| `test_server_upstream.py` | all four tiers, each `is_self`-guarded; cache invalidation on `mtime_ns` change; D18 — a non-loopback value at tier 2 or 3 is refused and names the offending file, while the same value at tier 1 is honoured |
| `test_upstream_reaches_socket.py` | the actual frozen-upstream defect: with the daemon running and **not** restarted, change the settings upstream between two requests and assert the second request arrives at the *second* in-process listener. Unmocked sockets — this is the one test that fails if resolution is merely re-parsed and cached without reaching the connection factory (`server.py:151-161`) and the derived sites (`:634`, `:767`, `:865`, `:869`, `:1065`). Every other row in this table passes against a naive string-only fix; this is what `Gemini-b9b.6` actually promises |
| `test_response_header_logging.py` | response-side header logging at `server.py:642` and `:877` is name-only at DEBUG, matching the request-side filter at `:446`/`:794`; a chained upstream's header *values* never reach the log |
| `test_dead_upstream.py` | connection refused → Anthropic-shaped body; per-tier wording, both variants literal (the `unchain` form for file-sourced, the unset-the-variable form for tier 1); the D18 refusal names the offending file; the resolved path is the server's own; the URL in every one of these bodies is re-serialized from the parsed `Upstream`, never interpolated raw; live upstream statuses pass through |
| `test_summarizer_follows.py` | the summarizer tracks a changed upstream; the explicit override still wins |
| `test_hook_output.py` | stdout is exactly one JSON object or empty; `systemMessage` present when displaced; the three-row suppression matrix keyed per `{project, url}` — including two projects, same displacing URL, both alerted; diagnostics never on stdout |
| `test_upstream_drift.py` | live `ROLLING_CONTEXT_UPSTREAM` differing from the recorded value fires the drift alert while `ANTHROPIC_BASE_URL` still points at us; the key going missing fires it; a second, different drift after the first is not suppressed |
| `test_status_verb.py` | reports chained/not-chained, the effective upstream and its source file, and reachability; exit codes distinguish healthy from displaced; it reads `/health` rather than re-deriving ; **`status` writes nothing** — a stale `refs` entry is reported, not pruned, and the state file and settings files are byte-identical after a `status` run; it takes no lock, so it does not serialize behind an in-flight `chain` |

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
| A numeric reference count for the shared upstream key | a bare integer maintained beside the records it counts can disagree with them, and it cannot answer *which* projects are still chained — which both the `unchain` message and `status` need. `shared_upstream.refs` is a list of projects, which is the count and the answer at once (D17) |
| Keeping the shared upstream key in `writes`, one entry per project, restoring the earliest recorded `displaced` | this was the first D17 draft and it is order-dependent: the entry holding the pre-rolling-context value is dropped when that project unchains, so if it leaves first the last project out restores our own chained value and the machine stays chained. The original has to live outside the per-project records, because those are destroyed on the way out (§5) |
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
This probe is the **first** implementation task, not a later one: the
`upstream-pinned-by-env` guard (§6), its refusal message, and the tier order all
depend on the outcome. Before implementing that guard, run a probe in the
style of the Fact 1/Fact 3 spikes — set both, observe which one a live session
actually uses — and record the result here as a measured fact. If the measurement
contradicts the assumption, the guard's refusal message and the tier order both
change.

### Review provenance

The design-review-gate ran to approval on all five reviewers: Product Manager and
Designer at `cdb1733`, Security at `bc2ea57`, CTO at `cd3d806`, Architect at
`891011b`. Later commits changed no verb, no state field, and no message the
earlier approvals rested on, except as noted below.

**Not independently reviewed.** Four strings were added after the Designer's
approval and four attempts to re-run that review died on harness faults, not on
findings. The user elected to proceed. They are:

- `status`'s stale block (`stale: <project> is recorded as chained…`)
- `unchain`'s still-referenced message (`left ROLLING_CONTEXT_UPSTREAM set — still
  chained by: …`)
- the `declined` guard message
- the `write-target-escapes-project` guard message

Each follows the conventions the Designer did approve — lowercase, no terminal
punctuation, state-then-remedy, bare verb for terminal readers. One known nit,
worth fixing while implementing rather than tracking separately:
`write-target-escapes-project` names the mechanism but not the consequence, unlike
every other row in that table, so a user cannot tell from it whether their project
is broken or merely unchained.

Next step after §12's probe is recorded: `writing-plans`.
