# Rolling Context — Design Brief

*How the proxy works, why its thresholds sit where they do, and an honest account of what it costs.*

---

## The one-paragraph version

Claude Code re-sends your **entire** conversation to the API on every turn, and as a session grows that prefix gets re-billed turn after turn. The built-in `/compact` already fixes the *cost* of that — it caps the prefix — but it does so by throwing the whole conversation away and replacing it with a lossy summary, so after a few compactions you're reasoning from a summary of a summary. Rolling Context sits between Claude Code and Anthropic as a tiny, zero-dependency proxy. When a conversation crosses a token threshold, it summarizes the **old** turns into one continuously-merged timeline and keeps the **recent** turns byte-for-byte intact — so aggressive compression doesn't cost you the live working set. **This is a retention tool, not a cost-saver.** Measured against `/compact`, it costs a *modest premium*, not a saving: you pay a little more to keep the recent detail exact. No API key, no config, no latency on the critical path.

---

## 1. The problem, stated in money

Every token you carry in context is re-billed on every turn — at cache-read rates once caching kicks in. So a session's cumulative input cost is **the sum of the prefix size over all turns**. Left unmanaged, that prefix only grows, so cost climbs faster than linearly with session length: each new turn is billed against an ever-larger prefix. A larger context window doesn't fix this — it just raises the ceiling the prefix climbs toward before anything caps it.

A second effect sharpens this. **Cache misses:** the prompt cache has a TTL (5 min default). Read a diff, get coffee, come back, and the next turn re-*writes* the whole prefix at the 1.25× write rate instead of reading it at 0.1×. The larger the prefix, the more a single cold turn costs.

(Earlier Sonnet-4/4.5-era 1M models added a third force — a `2× input / 1.5× output` premium on everything above 200K prompt tokens. Current Opus and Sonnet price the full 1M window [flat][price], so that penalty band no longer applies; the numbers below assume flat pricing.)

Capping the prefix bends that super-linear curve back toward a line — **but Claude Code's built-in `/compact` already caps the prefix.** The money problem is solved in the box. What `/compact` *pays* for that cap — discarding the whole conversation each time it fires — is the actual problem this proxy addresses. So read this section as *the mechanism*, not the pitch: §5 is blunt that against the real baseline the proxy costs slightly more, not less.

---

## 2. How it works

The proxy is a localhost HTTP reverse proxy (Python stdlib, no pip deps). Installation points Claude Code at it by setting `ANTHROPIC_BASE_URL` to `http://127.0.0.1:5588`; the proxy forwards to the real API. It is **stateless** and **content-addressed** — it has no notion of sessions, users, or subagents. It only sees request bodies and matches them by content hash.

```mermaid
sequenceDiagram
    participant CC as Claude Code
    participant P as Rolling Context proxy (:5588)
    participant API as api.anthropic.com

    CC->>P: POST /v1/messages  (full messages array)
    P->>P: hash incoming messages → find_match?
    alt a stored compression matches
        P->>P: swap messages = [summary, ack] + verbatim tail
    end
    P->>API: forward (rewritten or untouched)
    API-->>P: streamed SSE response
    P-->>CC: stream passthrough (zero added latency)
    P->>P: parse real input_tokens from response usage
    alt tokens > TRIGGER and not already compressing
        P-)P: background thread: summarize old turns,<br/>store result keyed by content hash
    end
```

Key properties fall straight out of the content-hash design:

- **Multiple conversations, subagents, branches — all just work.** Each has unique content → unique hashes → its own independent compression entry. A subagent that crosses the threshold is compressed on its own; nothing bleeds between conversations.
- **Never blocks.** The client is served the upstream stream first; summarization runs in a background thread and is applied on the *next* request.
- **Nothing to corrupt.** Restart the proxy anytime; worst case is one extra compression cycle.
- **Transcripts preserved.** Claude Code still writes full JSONL locally; the proxy only rewrites what goes *over the wire*.

**What compression does to the array.** When a conversation's real input tokens (read from the upstream response's `usage`) exceed the trigger, the proxy summarizes `messages[0:cut]` into a structured timeline (Active Goal / Timeline / Current State / Key Details) and produces `[summary, ack] + recent_verbatim`. The cut is chosen so it never splits a tool-use/tool-result pair and never orphans the current task. On the next request, the proxy hashes the incoming messages, finds the stored compression, and swaps the summary prefix in front of the still-verbatim tail — preserving Claude Code's own cache breakpoints on that tail.

**Summarization is nearly free** in the default "native" mode. The proxy clones the session's own request (same model, system, tools, truncated at the cut), so Anthropic serves it as a [prompt-cache][cache] read: a few hundred fresh tokens to summarize a ~70K span, not a second full pass. Because the request keeps the session's own shape, it also clears the subscription-OAuth classifier that would 429 a naked side request.

**Keeping the summary from saturating.** A rolling summary that only ever grows carries a failure mode inside it. The original contract told the summarizer to copy the previous summary forward unchanged and append the new events after it. Pair "copy forward, then append" with a fixed output cap and you get a trap: once the carried-forward text alone approaches the cap, there's no room left for the newest events, and since the old text is written first, it's the *newest* work that gets cut. The summary saturates and starts dropping the very turns it exists to protect. How often real sessions reached that ceiling was never billed or measured — but it's a structural certainty for any session long enough to fill the cap, not a tail risk.

The fix swaps "copy forward, append" for **oldest-first decay** on a tiered contract. Three things never shrink: the Active Goal, the user's stated constraints, and the Key Details. Only the Timeline decays, and it decays from the *old* end. Recent steps stay detailed; as the summary nears its budget, the oldest steps merge into denser milestone bullets. The newest events are never the ones sacrificed.

A prompt alone can't guarantee that, because it rests on the model obeying instructions. So the size bound lives in code, not in goodwill. Two budgets do the work: a ~16K-token soft target the prompt asks for, and a 20K-token hard ceiling set as the real `max_tokens`. After each summarization the proxy reads the API's `stop_reason`; if the model hit the token cap, or the returned summary still measures over the ceiling, it runs exactly one condense pass — re-summarizing under the same tiered contract, folding the oldest Timeline, keeping the invariants. One pass, never a loop. Both summarizer paths, the default native mode and the flattened fallback, carry the same guard.

```mermaid
flowchart TD
    S[summarize old turns] --> P{truncated at cap<br/>OR over 20K ceiling?}
    P -- no --> R[return summary]
    P -- yes --> C[one condense pass:<br/>fold oldest Timeline,<br/>keep invariants]
    C --> R
```

**What's proven, and what isn't.** The size bound is deterministic and tested: the guard fires on a truncated `stop_reason` and on an over-ceiling measurement, runs a single condense pass, and leaves a normal summary untouched — checked for the native summarizer and both flattened wire formats ([`test_summary_decay_guard.py`](../tests/test_summary_decay_guard.py), [`test_flattened_guard.py`](../tests/test_flattened_guard.py)), with the SSE `stop_reason` parse covered on its own ([`test_sse_stop_reason.py`](../tests/test_sse_stop_reason.py)). What a mock backend can't prove is the other half: that a real model, told to shed oldest-first, actually does. That waits on a live check — one long session run through repeated compressions, confirming from the proxy log that summary size settles at or below the soft target and the guard rarely fires. The code guarantees the summary stays bounded; the contract, not yet independently confirmed, is what aims that bound at the oldest material.

---

## 3. The keep policy: turns, not just tokens

The recent tail is chosen by a **blend**, not a flat token budget: keep the last *N* whole user-turns (`ROLLING_CONTEXT_KEEP_TURNS`, default 8), never fewer than a floor (`ROLLING_CONTEXT_KEEP_FLOOR`, default 3), clamped by the `TARGET` token ceiling (`ROLLING_CONTEXT_TARGET`, default 40K).

Why blend rather than a flat token count? Because a single turn's size varies ~40× (in the telemetry that calibrated this: median 1.6K tokens, p95 11K, p99 **67K** — a giant file read). A flat token budget mis-allocates against that spread: on cheap stretches it hoards ~25 stale small turns; on a big-dump turn it blows the entire budget on **one** turn and summarizes away the reasoning that surrounds it. Choosing by *whole turns* keeps the unit of coherence intact — a mid-task tool chain is never split — while the token ceiling still caps cost. The floor guarantees you never keep just one giant dump with none of its context.

---

## 4. Why the thresholds sit where they do

The two numbers that matter are the **trigger** (100K) and the **keep** (blend around 40K). Neither was guessed. They were chosen against a first-principles cost model calibrated to real session telemetry, using the API's [published relative pricing][price]. The model and its inputs are committed under [`docs/cost-model/`](cost-model/) — every figure below is reproducible.

### Relative pricing (the units the model runs in)

| Operation | Cost, relative to fresh input |
|---|---|
| Fresh input token | 1× |
| Prompt-cache **read** | 0.1× |
| Prompt-cache **write** | 1.25× (5-min TTL) |
| Output token | 5× |

(Opus 4.8 prices the full 1M window flat — there is no >200K long-context multiplier to model. The cache-read rate is what makes an unmanaged prefix expensive: cheap per turn, but paid on the whole prefix, every turn.)

### The trade-off the trigger balances

Compression isn't free: rewriting the prefix **invalidates the prompt cache** for everything downstream, so the first turn after a compression re-writes the kept tail at 1.25× instead of reading it at 0.1×. On top of that, the summary itself is billed as output, up to the 16K soft cap — the single largest slice of each compaction's cost. That overhead pushes toward compressing **rarely** (higher trigger, shed more each time). Meanwhile, carrying a bigger prefix every turn at 0.1× pushes toward compressing **early** (lower trigger). The optimum is where those meet.

Replaying the **full current mechanics** — blend keep, the rolling summary that grows and saturates the 16K cap, the decay guard, and the cache-invalidation penalty — over **1,940 real sessions (114K turns)** puts the cost optimum at **exactly 100K** ([`fullmech.py`](cost-model/fullmech.py)). It is a flat-bottomed basin: within 1% of optimum from **90K–120K**, within 2% from 80K–130K. The practical read:

- **100K is the optimum, and it's the value the tool ships.** The optimum stays in 90–120K across every summary-size assumption swept.
- Going **too low** is the real mistake: a 50K trigger costs ~36% more, because you pay the tail-rebuild penalty *and* the summary-output cost too often. Pushing to 200K costs ~11% more — you then carry the extra prefix on every turn.
- This is the cheapest way to run *the proxy*. It does **not** mean the proxy is cheaper than `/compact` — see §5.

**Session length is why 100K, and not lower.** Only **~28% of real sessions ever reach 100K** of context; the median tops out near 74K and never compacts at all ([`length_cond.py`](cost-model/length_cond.py)). Among the sessions that do cross, the amortization break-even — how many more turns a compaction needs before its overhead pays back — is **~70–100 turns**, dominated by that up-to-16K summary output. That break-even nearly equals the **median remaining length at 100K (~74 turns)**, which is exactly the condition that pins the optimum there: trigger any lower and you compact sessions that end before they amortize; any higher and long sessions carry an oversized prefix. Conditioning the trigger on how long a session has *already* run adds almost nothing (< 0.1% in replay, [`adaptive.py`](cost-model/adaptive.py)) — because reaching a high context is itself the signal of a long session.

On the keep side, the blend (`N=8, floor=3, ~40K cap`) earns its extra complexity on a replay of the real sessions that cross the trigger ([`replay.py`](cost-model/replay.py)). At the **same 5-turn coverage** (99.3%) it costs **~12% less** than a flat 40K-token budget *of the same policy*, and it removes that policy's 1-in-6 "keep only a giant dump" coherence hazard: 16% of flat-policy compressions collapse to a single kept turn, against 0% for the blend. (This is a keep-policy comparison — blend vs. flat, both inside the proxy — not a comparison against `/compact`.)

---

## 5. What it costs — and why cost is not the reason to run it

**What this is:** a first-principles cost model in the API's real relative units, driven by **1,940 real Claude Code sessions (114K turns)** and the **full current mechanics** — the blend keep policy, a rolling summary that grows with the dropped history and saturates its 16K soft cap (the guard fires on ~37% of compactions), and the cache-invalidation penalty each compression pays. It is a model calibrated to real usage, not a live billing A/B, so treat every figure as a grounded estimate, not an invoice. Scripts under [`docs/cost-model/`](cost-model/): [`fullmech.py`](cost-model/fullmech.py), [`summ_rate.py`](cost-model/summ_rate.py), [`setpoint.py`](cost-model/setpoint.py).

**The headline, stated plainly: this proxy does not save money against Claude Code's built-in `/compact`. It costs modestly more.** Both cap the prefix, so both make a long session's input cost grow linearly rather than quadratically. The difference is what each keeps. Native `/compact` discards the whole conversation and keeps only a summary. The proxy additionally keeps a verbatim recent tail, *and* produces a summary, *and* — because that tail raises the floor it compacts back down to — fires somewhat more often. More work costs more.

Measured from **658 real native compactions** in the telemetry, Claude Code's own compaction fires at a **median context of 129K (mean 153K)** ([`setpoint.py`](cost-model/setpoint.py)). Modeling both policies over the same 1,940 sessions:

| Comparison (modeled, full mechanics) | Result |
|---|--:|
| Proxy @100K vs. native `/compact` at its measured median (~129K) | **~15% more expensive** |
| Proxy @100K vs. native `/compact` at a high setpoint (~160K) | **~6% more expensive** |
| Proxy @100K vs. "never compact — carry everything" | ~80% cheaper |

The last row is real arithmetic against a baseline **nobody can run** — an unmanaged prefix exceeds the context window and re-reads on every turn. Quoting it as a "saving" would be dishonest. The only baseline that matters is Claude Code's own compaction, and against that the proxy is a **single-digit-to-~15% premium**, not a saving.

**Correcting the record.** An earlier version of this brief claimed the proxy was *~12% cheaper* than native compaction. That number came from a mislabeled baseline in the cost model: the "native compact" row actually kept a 40K verbatim tail — i.e. it was the proxy's *own* policy at a higher trigger, not native compaction at all. True native compaction keeps no verbatim tail. Against it, the sign flips. The old claim is retracted.

**The one lever that could reverse it — and why subscription users can't pull it.** The proxy's per-compaction cost is dominated by the summary *output*, billed at the session model's output rate (Opus, $25/MTok). Point summarization at a cheaper model — Haiku, 5× cheaper output — and the picture changes: on **token/API billing**, a flattened Haiku summarizer at ~100K with a ~25K tail lands cost-neutral-to-cheaper than native `/compact` *while still keeping a verbatim tail*. Native `/compact` structurally cannot do this — it is locked to the session model. But **that lever is unavailable on a Pro/Max subscription, which is how this plugin is actually used.** Native mode forces the session model precisely so the cloned request passes Anthropic's subscription-OAuth classifier; routing a cheaper model requires flattened mode, whose bare, non-session-shaped request is exactly what that classifier is built to reject (the reason native mode exists — see the compressor module header's issue-#4 note). It is untested against a live backend, but the strong prior is rejection. **So for subscription users there is no cheaper-summarizer path, and the proxy is a budget premium over `/compact`, full stop.**

**On a subscription, "cost" is rate-limit budget, and the proxy burns more of it.** Nothing here is dollars on Pro/Max. But the same curves govern how fast you burn the rate-limit window, and the proxy — carrying the tail and compacting more often — spends **more** of that window than native `/compact`, by the same ~6–15%. If your only goal is to stretch the rate-limit window, native `/compact` (optionally at a lower threshold) is cheaper, and this proxy is the wrong tool.

**So why run it at all? Retention quality — and only that.** Native compaction replaces the whole conversation with a summary each time it fires, so at an aggressive threshold you are soon reasoning from a summary of a summary. The proxy keeps the recent tail **byte-for-byte** and summarizes only the old span, which is what lets you compress early (100K) without that degradation. The summary quality on the *old* material is comparable to native's — both are summaries — but the recent tail, the part you're actively working in, stays exact. That is the whole value proposition, and it costs the premium above. If you value keeping the live working set intact, the premium buys something real. If you want lower spend, it does not.

**Thinking tokens shift the percentage, not the dollars.** Extended-thinking tokens are billed as output and are *compression-invariant*: you generate the same reasoning for the current task regardless of prefix size, and Claude Code drops prior-turn thinking from the context it resends, so it never accumulates in the growing prefix at all. The model prices output at the telemetry median (≈330 tok/turn, i.e. light thinking). Heavier thinking (2–4K tok/turn) leaves the premium's dollar magnitude essentially unchanged while shrinking it as a percentage, because thinking inflates the shared denominator ([`think_sens.py`](cost-model/think_sens.py)).

Two further honest boundaries:

- **Short sessions are a wash.** Under ~100K of accumulated context — a 20-minute task — there is little to compress and the overhead isn't worth it. The tool is designed to do nothing there.
- **The proxy's own overhead is negligible.** Hashing the message array costs about **0.8 ms/request** at the operating point (~90K), and the upstream TLS handshake ~40 ms; both are dwarfed by the 2–15 second LLM stream they sit behind. Two proposed micro-optimizations were measured, then deliberately left unbuilt ([`profile_hash.py`](cost-model/profile_hash.py)).

---

## 6. Where it doesn't help (and why that's fine)

- **Cost is not the differentiator — quality under repetition is.** Lowering Claude Code's own auto-compact threshold buys a **cheaper** cost curve for free: native compaction with no verbatim tail is strictly cheaper than this proxy (§5). What it can't buy is the rolling-verbatim property — built-in compaction replaces the whole conversation each time it fires, so at a low threshold you're soon working from a summary of a summary. Rolling Context exists so aggressive compression doesn't cost you the session; it charges a small premium for that.
- **On a subscription it is a net cost, not a saving.** This is the honest headline for how the plugin is actually used. The proxy spends ~6–15% more of the rate-limit window than native `/compact`, and the cheaper-summarizer escape that would close the gap is blocked by the subscription-OAuth classifier (§5). Run it because you want the retention, not because you want to spend less.
- **It cannot preserve the prompt cache *and* clear server-side.** Anthropic's native [Context Editing][ctxedit] clears old tool results *after* cache lookup (cache-preserving) but only *drops* content, with no summary. This proxy summarizes, but rewrites client-side and so invalidates the cache. The two fight on the cache axis, and you can only have one owner of tool-output lifecycle. Combining them is future work behind a different architecture, not a config flag.
- **The oldest-first contract leans on the model.** Code guarantees the summary can't grow past the ceiling; *which* material a tight compression sheds is the model following the decay prompt. The guard bounds size, not editorial judgment. If a summarizer ignores the contract and cuts the newest on its first pass, the condense pass re-summarizes already-truncated text and can't bring back what was dropped. The live-backend check (§2) is the honest gate before trusting that behavior.

---

## References

- **Anthropic pricing** — per-token input/output and prompt-cache rates (and the legacy >200K long-context tier, now retired for current models): <https://platform.claude.com/docs/en/about-claude/pricing>
- **Prompt caching** — cache read/write rates and the 5-minute TTL the trigger economics turn on: <https://platform.claude.com/docs/en/build-with-claude/prompt-caching>
- **Context editing** — Anthropic's native, cache-preserving tool-result clearing (§6): <https://platform.claude.com/docs/en/build-with-claude/context-editing>
- **Cost model & telemetry** — the scripts behind every §4–§5 figure: [`docs/cost-model/`](cost-model/)

[price]: https://platform.claude.com/docs/en/about-claude/pricing
[cache]: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
[ctxedit]: https://platform.claude.com/docs/en/build-with-claude/context-editing

---

## Appendix: code map (stable references)

| Concern | Location |
|---|---|
| Proxy handler, request interception, streaming | `proxy/server.py` → `ProxyHandler._handle_messages` |
| Trigger check (real usage > threshold) | `proxy/server.py` → `_handle_messages` (`total_input > TRIGGER_TOKENS`) |
| Atomic single-flight compression reservation | `proxy/server.py` → `CompressionStore.try_begin_compression` |
| Content-hash match / injection | `proxy/server.py` → `CompressionStore.find_match`, `_hash_messages` |
| Bounded store (cap + LRU evict) | `proxy/server.py` → `CompressionStore._evict_locked` |
| Rolling compression orchestration | `proxy/compressor.py` → `RollingCompressor.compress` |
| Blend keep-cut selection | `proxy/compressor.py` → `RollingCompressor._find_keep_index` |
| Native (prompt-cached) summarization | `proxy/compressor.py` → `RollingCompressor._summarize_native` |
| SSE parse + `stop_reason` capture | `proxy/compressor.py` → `RollingCompressor._parse_summary_sse` |
| Summary decay guard (condense on truncation/over-ceiling) | `proxy/compressor.py` → `_summarize_native`, `_condense_summary` |
| Flattened summarize + same guard | `proxy/compressor.py` → `_summarize_flattened`, `_summarize_flattened_once` |
| Tiered-decay + condense prompts | `proxy/compressor.py` → `SUMMARY_RULES`, `NATIVE_COMPACT_PROMPT`, `CONDENSE_PROMPT` |
| Install / `ANTHROPIC_BASE_URL` wiring | `hooks/start-proxy.sh`, `install.sh` |
| Configuration (all env vars) | `README.md` → Configuration |
| The economics, in full | `README.md` → "The economics: capping the prefix, and what it costs" |

*Defaults:* `ROLLING_CONTEXT_TRIGGER=100000`, `ROLLING_CONTEXT_TARGET=40000`, `ROLLING_CONTEXT_KEEP_TURNS=8`, `ROLLING_CONTEXT_KEEP_FLOOR=3`, `ROLLING_CONTEXT_STORE_MAX=32`.
