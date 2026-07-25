# Rolling Context for Claude Code

[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.7+](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org)
![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-orange.svg)

A transparent proxy that gives Claude Code **rolling context compression** — old messages get automatically summarized while recent messages stay fully verbatim. You never hit the context wall, and you never lose important details.

**Zero config.** Uses your existing Claude Code auth. No API key needed. Just install and forget.

> 🍴 **Fork** of [NodeNestor/claude-rolling-context](https://github.com/NodeNestor/claude-rolling-context) (MIT), substantially extended and maintained by [davdittrich](https://github.com/davdittrich/claude-rolling-context) — a turn-aware keep policy, concurrency hardening, correctness fixes, and a stdlib test suite. See [What this fork adds](#what-this-fork-adds).

> Claude Code's built-in `/compact` replaces your **entire** conversation with a lossy summary. After a few compactions, you're summarizing a summary of a summary. This plugin only compresses old messages — recent context stays untouched.

**This is a retention tool, not a cost-saver.** Claude Code's built-in `/compact` already caps the prefix, so the cost problem is handled in the box. Measured against `/compact` **at the same auto-compact threshold**, this proxy always costs **more**, not less — about +23% at its 100K default (from +6% at lax triggers to +47% aggressive) ([the economics](#the-economics-capping-the-prefix-and-what-it-costs) shows why and how much). What you get for that premium is the recent conversation kept **verbatim** instead of discarded. If your goal is to spend less, use `/compact`. If your goal is aggressive compression that doesn't lose the session, use this. Not sure? [Should you use this?](#should-you-use-this) has the honest call.

## `/compact` vs Rolling Context

| | `/compact` (built-in) | Rolling Context |
|---|---|---|
| What gets compressed | Everything | Only old messages |
| Recent context | Summarized (lossy) | **Kept verbatim** |
| When it runs | Manual or near the context limit | Automatic, background |
| Latency impact | Blocks until done | Zero — async |
| After multiple compressions | Summary of summary of summary | Fresh rolling merge each time |
| Input cost over a long session | Capped → grows linearly | Capped → linear, but **higher at any matched trigger** (~+23% at 100K) |
| Original transcript | Replaced | Preserved (JSONL unchanged) |

## The economics: capping the prefix, and what it costs

No price cards needed — the argument works in **relative units** that hold for every Claude model. Take the model's fresh-input-token rate as `1×`. The API bills:

| Operation | Relative cost |
|---|---|
| Fresh input tokens | `1×` |
| Prompt-cache **read** | `0.1×` |
| Prompt-cache **write** | `1.25×` (5-min TTL) / `2×` (1-hour TTL) |
| Output tokens | `~5×` |

Claude Code re-sends the entire conversation on every turn. Even with caching working perfectly, each turn costs `prefix size × 0.1`. So a session's total input cost is **the sum of the prefix over all turns**:

- **Never compact (carry everything):** the prefix grows every turn, so total cost grows with the *square* of session length. This is the only case where the square-law bites — and nobody runs it, because a 900K+ prefix exceeds the window and re-reads at `0.1×` on every single turn.
- **Native `/compact` (the real baseline):** caps the prefix near the context limit, so total cost grows *linearly*. This already solves the cost problem — for free, in the box.
- **Rolling Context:** also caps the prefix, so also *linear* — but **higher than `/compact` at any matched trigger** (~+23% at the 100K default, +6% to +47% across the range), because it keeps a verbatim tail and therefore compacts more often. That premium buys retention, not savings.

**The cache-miss blast radius matters even more in interactive use.** The prompt cache has a TTL. Read a diff, think for a while, get coffee — and the next turn re-*writes* the whole prefix at `1.25×`. At a 900K prefix, one cold turn bills the equivalent of ~1.1M fresh input tokens. With the prefix capped at ~100K, the identical cold turn is ~9× cheaper. Compression doesn't just shrink the average turn — it caps the worst one.

**What compression itself costs:** each cycle re-writes the new (much smaller) prefix once, *plus* the summary itself as output — up to a 16K-token cap, the largest single slice. In native mode the summarization *input* is a cache read (a few hundred fresh tokens, measured — see below), but the summary output is billed in full. Net effect: at any matched auto-compact threshold the proxy costs **more** than native `/compact`, not less — ~+23% at the 100K default — because it does the same summarization *plus* carries a verbatim tail *plus* compacts more often. The full model, baselines, and the 100K-optimum derivation are in [the design brief](docs/design-brief.md) §4–§5. Short sessions are a wash; expect no cost change on a 20-minute task.

**On Pro/Max subscriptions** none of this is dollars — it's rate-limit budget, and the honest headline is that the proxy burns **more** of your window than native `/compact` at any matched threshold (~+23% at the 100K default), because it carries the tail and compacts more often. The one lever that could close the gap — summarizing on a cheaper model like Haiku — needs either a separate API key or a flattened request that the subscription-OAuth classifier rejects (the reason native mode exists). So on a subscription there is no way to make this cost-neutral. Run it for retention, not to stretch the window.

> **Honest note:** if cost were the *only* goal, this is the wrong tool — native `/compact` (optionally at a lower `CLAUDE_CODE_AUTO_COMPACT_WINDOW`) is **cheaper**, because it keeps no verbatim tail. What it can't buy is quality under repetition: built-in compaction replaces the whole conversation with a lossy summary every time it fires — at a low threshold it fires often, and you're soon working from a summary of a summary of a summary. The rolling design exists so aggressive compression doesn't cost you the session: recent work stays verbatim, and old work lives in one continuously-merged timeline instead of N generations of loss. You pay a small premium for that.

## Should you use this?

At a matched auto-compact threshold this proxy always costs **more** than the built-in `/compact` ([the economics](#the-economics-capping-the-prefix-and-what-it-costs)). So the question isn't whether it's cheaper. It's whether you need what the premium buys: the recent conversation kept **verbatim** under aggressive compression.

**Use it when:**
- You run genuinely long sessions (regularly past 100K). Under that, it does nothing.
- You compact early *and* need the recent working set exact — the code you just read, the exact error, the file you're editing. Native `/compact` can't give you that; it summarizes everything.
- You'll accept ~+23% over native (at a matched 100K trigger) for that retention.
- **API-key users:** you point summarization at a cheaper model (`ROLLING_CONTEXT_MODEL=claude-haiku-4-5`) and validate quality; the one path to cost-neutral *with* a verbatim tail.

**Avoid it when:**
- Cost or rate-limit budget is your binding constraint. Native `/compact` (optionally at a lower threshold) is cheaper, and on a subscription the cheaper-summarizer escape is blocked. Spend less by using `/compact`.
- Your work stays under ~100K. Nothing to compress.
- You depend on server-side prompt-cache preservation or native Context Editing — the proxy rewrites client-side and invalidates the cache; they fight.

**One-line verdicts:**
- **Subscription, want to spend less** → no. Use `/compact`.
- **Subscription, want recent detail kept exact under heavy compression** → yes, at a ~+23% rate-limit premium.
- **API-key, budget-conscious** → maybe: a Haiku summarizer can be cost-neutral-to-cheaper, but validate the summaries.

Full evidence, and the pin-proxy/raise-native tail case where the proxy does come out ahead, are in the [design brief](docs/design-brief.md) §5–§6.

## How It Works

```
Claude Code  ──►  Rolling Context Proxy (:5588)  ──►  Anthropic API
                         │
                         ├─ context < 100K tokens? pass through unchanged
                         │
                         └─ context > 100K tokens?
                              1. summarize old messages in the background
                                 (native mode: your session's own model,
                                  served almost entirely from prompt cache)
                              2. keep the last 3-8 recent user-turns verbatim
                                 (whole turns, capped at ~40K tokens)
                              3. inject compressed context on next request
                              4. never blocks, never adds latency
```

Instead of replacing everything, this plugin:

1. **Keeps recent messages untouched** — the last 3-8 recent user-turns stay verbatim (whole turns, so a mid-task tool chain is never split), capped at the `TARGET` token budget. The turn window is tunable via `ROLLING_CONTEXT_KEEP_TURNS` / `ROLLING_CONTEXT_KEEP_FLOOR`
2. **Only compresses when needed** — triggers at 100K (real API token count), compresses old messages, grows naturally until next trigger
3. **Merges summaries** — each compression cycle merges with the previous summary, building a rolling timeline
4. **Never blocks** — compression runs in the background, applied on the next request
5. **Full transcripts preserved** — Claude Code still saves everything to JSONL in `~/.claude/projects/`

## What this fork adds

This fork keeps the upstream design (transparent proxy, rolling summary, verbatim recent tail, zero deps) and adds:

- **Turn-aware blend keep policy** — instead of a flat token budget, the verbatim tail is chosen by *whole recent turns* (`ROLLING_CONTEXT_KEEP_TURNS` / `ROLLING_CONTEXT_KEEP_FLOOR`) clamped by the `TARGET` token ceiling. A mid-task tool chain is never split, and a single giant tool dump can't evict the surrounding reasoning. On real long-session telemetry this cut carried-tail cost while holding the working set.
- **Concurrency hardening** — compression reservations are atomic (parallel Claude Code requests can no longer trigger duplicate, billed summarizer calls), the pending→active promotion runs under the store lock, and completed results publish their match hashes before the guard flag so a finished compression is never dropped.
- **Bounded, non-leaking store** — the compression store is capped and LRU-evicts inactive entries (`ROLLING_CONTEXT_STORE_MAX`), and it no longer pins compressed-away message history in memory by default (`ROLLING_CONTEXT_DEBUG_MESSAGES`).
- **Correctness fixes** — non-streaming (`stream:false`) responses now parse real token usage; SSE usage parsing only touches the two events that carry it; a foreign `ROLLING_CONTEXT_MODEL` correctly switches to flattened mode (no silent prompt-cache miss); malformed/empty summarizer replies raise instead of injecting a `"None"` summary; message-shape guards prevent consecutive-user-turn and orphaned-`tool_result` 400s; native re-summarization carries the prior timeline forward rather than restarting it.
- **Bounded rolling summary** — the summary self-limits instead of growing forever. Its invariants (the Active Goal, your stated constraints, and Key Details) stay at full fidelity, while the oldest Timeline entries are merged into denser bullets as the summary nears a ~16K-token soft target. A 20K-token hard ceiling plus a deterministic condense pass — triggered on truncation or over-ceiling — guarantees the newest events are never the ones dropped.
- **A stdlib test suite** — 93 `unittest` cases, no pip deps:
  ```bash
  python3 -m unittest discover -s tests
  ```

## Install

### Option 1: Claude Code Plugin (recommended)

Run these two commands inside Claude Code:

```
/plugin marketplace add https://github.com/davdittrich/claude-rolling-context
/plugin install rolling-context
```

Restart your terminal and start a new Claude Code session. On the **first start**, the plugin configures `ANTHROPIC_BASE_URL` and starts the proxy. Since the env var only takes effect on the next terminal, **restart your terminal once more** — after that, everything works automatically. No pip install needed — pure Python stdlib.

### Option 2: Manual install

**Linux / macOS:**
```bash
git clone https://github.com/davdittrich/claude-rolling-context.git ~/claude-rolling-context
cd ~/claude-rolling-context
bash install.sh
```

**Windows (PowerShell):**
```powershell
git clone https://github.com/davdittrich/claude-rolling-context.git $HOME\claude-rolling-context
cd $HOME\claude-rolling-context
powershell -ExecutionPolicy Bypass -File install.ps1
```

The installer configures `ANTHROPIC_BASE_URL` and registers the plugin. Restart your terminal and you're done. Requires Python 3.7+ (no pip install needed — pure stdlib).

## How Compression Works

When the message array exceeds the trigger threshold:

```
BEFORE (hit 100K trigger):
  [msg1] [msg2] [msg3] ... [msg60] [msg61] ... [msg100]
  |<——————————————— ~105K tokens ——————————————>|

AFTER (compressed):
  [rolling summary] [ack] [msg61] ... [msg100]
  |<— ~5K summary —>|    |<—— verbatim ————————>|

NEXT CYCLE (grows back to 100K, triggers again):
  [rolling summary] [ack] [msg61] ... [msg140]
  |<——————————————— ~105K tokens ——————————————>|
  → new summary merges old summary + msg61-msg100
  → keeps msg101-msg140 verbatim
```

The summary preserves a structured record of everything that happened:

- **Active Goal** — what the user is currently asking for, constraints, do/don't rules
- **Previous Goals** — completed or shifted-away-from goals (kept brief)
- **Timeline** — chronological numbered steps: every file change, decision, error, and user instruction
- **Current State** — what's done, in progress, and next
- **Key Details** — file paths, configs, decisions that must survive compression

Goals evolve naturally across rolling compressions — the latest request stays prominent while completed goals move to the previous section. User instructions are never lost.

### How the summary is generated (native mode)

By default the proxy doesn't build a separate summarization request. It **clones the exact request Claude Code just sent** — same model, system prompt, and tools, with the conversation truncated at the cut point — and appends one user message asking for the summary (the same way Claude Code's own `/compact` works). Two big wins:

- **It's a prompt-cache read.** The cloned prefix was just sent by the chat request, so the API serves it from cache. Measured in practice: a ~72K-token compression request cost ~400 fresh input tokens.
- **It's genuine Claude Code session traffic.** Pro/Max subscription OAuth tokens are classified server-side — standalone requests that don't look like Claude Code get routed to the overage lane and rejected with 429. The cloned request passes because it *is* the session's own request shape.

Setting `ROLLING_CONTEXT_MODEL` pins a different summarizer model. Since a different model can't reuse the session's prompt cache anyway, this switches native mode off and sends a standalone flattened request to that model instead — same as configuring any `ROLLING_CONTEXT_SUMMARIZER_*` variable. Leave `ROLLING_CONTEXT_MODEL` unset to stay in native mode and compress with the session's own model — see below.

### Using any API or a local model for compression

Summarization can run on a completely separate endpoint — any Anthropic-format API, or any OpenAI-compatible one (Ollama, LM Studio, vLLM, OpenRouter, DeepSeek, Groq, ...):

```bash
# Separate Anthropic API key (billed there instead of your subscription)
export ROLLING_CONTEXT_SUMMARIZER_KEY=sk-ant-api03-...

# Local model via Ollama / LM Studio / vLLM (OpenAI-compatible)
export ROLLING_CONTEXT_SUMMARIZER_URL=http://127.0.0.1:11434
export ROLLING_CONTEXT_SUMMARIZER_FORMAT=openai
export ROLLING_CONTEXT_MODEL=qwen3:8b   # required for openai format

# OpenRouter (or any hosted OpenAI-compatible API)
export ROLLING_CONTEXT_SUMMARIZER_URL=https://openrouter.ai/api
export ROLLING_CONTEXT_SUMMARIZER_FORMAT=openai
export ROLLING_CONTEXT_SUMMARIZER_KEY=sk-or-...
export ROLLING_CONTEXT_MODEL=deepseek/deepseek-chat
```

## Architecture

The proxy is **fully stateless** — no sessions, no databases, no tracking. It works by hashing message content:

1. When a response comes back from the API with a high token count, the proxy compresses the messages and stores the result keyed by content hashes
2. On the next request, it hashes the incoming messages and checks for a matching compression
3. If found, it swaps in the compressed version transparently

This means:
- **Multiple conversations work automatically** — each conversation has unique content, unique hashes, no collision
- **Subagents and branches just work** — the proxy doesn't care about sessions, only content
- **No state to corrupt** — restart the proxy anytime, worst case is one extra compression cycle
- **Concurrent-safe and bounded** — compression reservations are atomic, so parallel requests can't trigger duplicate summarizer calls, and the store is capped (`ROLLING_CONTEXT_STORE_MAX`, oldest non-active entry evicted) so it never grows without bound
- **Claude Code sees nothing different** — the proxy is invisible, JSONL transcripts are unmodified

## Configuration

All settings via environment variables (all optional — defaults work great):

| Variable | Default | Description |
|----------|---------|-------------|
| `ROLLING_CONTEXT_TRIGGER` | `100000` | Compress when context exceeds this many tokens |
| `ROLLING_CONTEXT_TARGET` | `40000` | Soft token ceiling for the recent messages kept after compression |
| `ROLLING_CONTEXT_KEEP_TURNS` | `8` | Max recent user-turns kept verbatim after compression |
| `ROLLING_CONTEXT_KEEP_FLOOR` | `3` | Min recent user-turns always kept, even when one turn exceeds `TARGET` |
| `ROLLING_CONTEXT_MODEL` | *(session model, native)* | Summarizer model; unset = native mode, session's own model (prompt-cache hit); set = forces flattened mode to this model |
| `ROLLING_CONTEXT_PORT` | `5588` | Proxy listen port |
| `ROLLING_CONTEXT_UPSTREAM` | `https://api.anthropic.com` | Upstream API URL (chain to another proxy!) |
| `ROLLING_CONTEXT_SUMMARIZER_URL` | *(upstream)* | Custom endpoint for summarization (local model, other API) |
| `ROLLING_CONTEXT_SUMMARIZER_KEY` | *(uses Claude Code auth)* | API key for custom summarizer endpoint |
| `ROLLING_CONTEXT_SUMMARIZER_FORMAT` | `anthropic` | `openai` = /v1/chat/completions for OpenAI-compatible endpoints |
| `ROLLING_CONTEXT_FAILURE_COOLDOWN` | `300` | Seconds to wait before retrying after a failed compression |
| `ROLLING_CONTEXT_STORE_MAX` | `32` | Max stored compression entries; oldest non-active entry is evicted on insert once over cap |
| `ROLLING_CONTEXT_DEBUG_MESSAGES` | *(off)* | Set `1`/`true` to retain the compressed-away original messages per entry (capped, for mismatch debugging) — off by default to avoid pinning message history in memory |

## Proxy Chaining

Already using another proxy (model router, API gateway, etc.)? Rolling Context auto-detects this and chains through it:

```
Claude Code  ──►  Rolling Context (:5588)  ──►  Your Proxy  ──►  Anthropic API
```

If `ANTHROPIC_BASE_URL` is already set when you install, the plugin automatically saves it as `ROLLING_CONTEXT_UPSTREAM` and inserts itself in front. No manual config needed.

You can also set it explicitly:
```bash
export ROLLING_CONTEXT_UPSTREAM=http://localhost:8080  # your existing proxy
```

## Health Check

```bash
curl http://127.0.0.1:5588/health
```

Returns compression stats and runtime info. Fields include:

- `version` — the running proxy version, read from `.claude-plugin/plugin.json` (`"unknown"` if unreadable).
- `compression_count`, `total_tokens_saved`, `stored_compressions`, `active_compressions` — aggregate compression stats.
- `recent_requests` — the last 3 routed requests, newest first. Each entry: `ts` (ISO 8601 local datetime string, e.g. `2026-07-25T16:30:00+02:00`), `before_chars` (context received from Claude Code), `after_chars` (context forwarded upstream after any compressed-prefix injection; equals `before_chars` when nothing was injected), `injected` (bool), `after_tokens` (real upstream-reported input tokens for the forwarded request; `0` when upstream reported none — never an estimate).
- `last_compression` — the most recent compression event, or `null` before any has run. Keys: `ts` (ISO 8601 local datetime string, e.g. `2026-07-25T16:30:00+02:00`), `before_chars` and `after_chars` (exact context size in vs out of the compression), `before_tokens` (real token count that triggered it; `0` if unknown). No after-token count — compression output tokens are only estimable.
- config echo: `trigger_tokens`, `target_tokens`, `keep_turns`, `keep_floor`, `summarizer_model`, `summarizer_mode`, `upstream_url`.

## Debug

```bash
curl http://127.0.0.1:5588/debug/compressions
```

Returns the stored compression entries with their full summary content — useful for verifying what the rolling summary captured and whether user goals/instructions survived compression. The store is capped at `ROLLING_CONTEXT_STORE_MAX` entries (oldest evicted first); per-entry retention of the original compressed-away messages is off by default (`ROLLING_CONTEXT_DEBUG_MESSAGES`).

## Uninstall

Run the uninstall script — it handles both manual and marketplace installs, stops the proxy, cleans env vars, and removes all plugin registrations.

**Linux / macOS:**
```bash
cd ~/claude-rolling-context && bash uninstall.sh
```

**Windows (PowerShell):**
```powershell
cd $HOME\claude-rolling-context; powershell -ExecutionPolicy Bypass -File uninstall.ps1
```

If you installed via marketplace and already deleted the repo, you can run it from the cache:
```powershell
cd $HOME\.claude\plugins\marketplaces\rolling-context
powershell -ExecutionPolicy Bypass -File uninstall.ps1
```

## Credits & License

Originally created by [NodeNestor](https://github.com/NodeNestor/claude-rolling-context). This fork is substantially extended and maintained by [davdittrich](https://github.com/davdittrich/claude-rolling-context) — see [What this fork adds](#what-this-fork-adds).

MIT — © 2026 NodeNestor (original), © 2026 davdittrich (fork modifications). See [LICENSE](LICENSE).
