# Bounded Tiered-Decay Rolling Summary — Design Spec

**Date:** 2026-07-25
**Status:** Approved (design); implementation not started
**Component:** `proxy/compressor.py`
**Related:** `docs/design-brief.md` (mechanism overview), `docs/cost-model/` (telemetry)

---

## 1. Problem

The rolling summary is **append-only and hard-capped**, and those two properties collide.

- **Append-only contract.** `NATIVE_COMPACT_PROMPT` (compressor.py:143) instructs the model to *"reproduce its Timeline and Key Details content VERBATIM, unchanged... only APPEND the new events... Never paraphrase, re-summarize, reorder, or drop any entry... copy it forward exactly, then extend it."* The flattened path carries the same contract (compressor.py:617-620, *"keep all details, extend the timeline"*). Under this rule the `## Timeline` section can only grow. A long session accumulates dozens of numbered steps (observed: 69 in one session).
- **Hard output cap.** Both summarizers set `max_tokens = 16000` (`_summarize_native` compressor.py:421; `_summarize_flattened` compressor.py:496). When the ever-growing verbatim carry-forward fills that budget, generation stops at the cap.

**Failure mode:** because old entries are copied *first* (verbatim, by contract) and new events appended *last*, hitting the cap truncates the **newest**, most-relevant material — the exact opposite of what a rolling summary should drop. Frequency is estimated at roughly 1 in 4 long sessions (modeled from telemetry, not measured via billing A/B; flagged as estimate).

The root cause is the append-only contract, not the cap size. Raising the cap only delays saturation.

## 2. Goals / Non-goals

**Goals**
- Summary size **self-stabilizes below a ceiling** across unbounded session length — saturation becomes impossible by construction.
- When compression must shed material, it sheds the **oldest** Timeline detail, never the newest.
- A small **Invariants tier** (goals, user constraints, key decisions/paths) is preserved at full fidelity indefinitely.
- **Zero new external state or dependencies** — state rides entirely in the summary text (stateless, content-addressed proxy unchanged).
- Prompt-cache friendliness preserved (summary prefix still changes only per compression).

**Non-goals**
- No external store, vector DB, embeddings, or memory tool-calls (envelope B — ruled infeasible from a proxy: MemGPT/RAG/LLMLingua/native-memory all require a client-side capability a proxy cannot force; see design brief §6 and SOTA analysis).
- No change to the 100K trigger or the keep-window blend policy (both locked by prior decisions).
- No mechanical parsing/surgery of the numbered Timeline (brittle; superseded by the recursive-fold backstop).

## 3. Design — A2-hardened (structured tiered decay + deterministic guard)

### 3.1 Key realization: no new tier engine

`SUMMARY_RULES` (compressor.py:103-132) already emits sections: `## Active Goal`, `## Previous Goals`, `## Timeline`, `## Current State`, `## Key Details`. The tiers already exist as markdown the model maintains each compression. The fix is to **change what the prompt tells the model to do with the oldest section**, and to add a code-side guarantee — not to build a tier-parsing subsystem in the proxy.

### 3.2 Tier contract (prompt changes)

Rewrite the decay contract in **three places**: `SUMMARY_RULES` (compressor.py:103-132), `NATIVE_COMPACT_PROMPT` (compressor.py:143), and the flattened existing-summary section (compressor.py:617-620). Replace append-only with **oldest-first generational decay**:

| Tier | Sections | Policy |
|------|----------|--------|
| **Invariants** (never decay) | `## Active Goal`, stated user constraints, `## Key Details` | Full fidelity, always. Never condensed, never dropped. |
| **Recent Timeline** | last ~15–20 `## Timeline` steps | Detailed. |
| **Aged Timeline** | older `## Timeline` steps | When the summary approaches budget, **merge adjacent oldest entries and abstract them into denser milestone bullets**. |
| **Decayed goals** | `## Previous Goals` | Already "keep brief" — unchanged. |

Contract language (replaces the "copy forward VERBATIM / never drop" wording):
> Carry the prior summary forward. Keep the Active Goal, user constraints, and Key Details at full fidelity — never condense or drop them. Keep recent Timeline entries detailed. As the summary approaches its size budget, **merge and abstract the OLDEST Timeline entries into denser milestone bullets** rather than dropping the newest. Stay within ~16K tokens.

**Budgets:** soft target **16K tokens** (operating point the prompt aims for), hard ceiling **20K tokens** (`max_tokens`, raised from 16000). With decay active, steady-state size sits at/below the soft target; the ceiling is a true safety limit, not the operating point.

### 3.3 Deterministic guard (the "hardened" part)

The prompt alone is model-goodwill (LangChain's `ConversationSummaryBufferMemory` proves models don't reliably self-bound a summary). Add a code-enforced backstop. **Scope: native path first** (`_summarize_native`, the default/primary path); the identical guard on `_summarize_flattened` is a **fast-follow** (decided 2026-07-25). The prompt-contract changes (§3.2) still apply to both paths on day one — only the code guard is native-first.

1. **Capture `stop_reason`.** The native SSE loop (compressor.py:472-484) currently parses `message_start`, `content_block_delta`, `error` — but **not** `message_delta`, which carries `delta.stop_reason`. Add a branch to capture it. (Flattened path: read `stop_reason` from the parsed response.)
2. **Trigger condition:** `stop_reason == "max_tokens"` **OR** measured summary size > hard ceiling (20K).
3. **Action — one recursive condense pass:** re-summarize the returned summary with an explicit instruction: *"Compress this summary under 16K tokens. Keep the Active Goal, user constraints, and Key Details verbatim. Fold the oldest Timeline entries into denser bullets."* Cap at **1 retry**. If still over, accept and log — the recursive pass condenses *oldest*, so the newest is never the casualty.
4. **Observability:** increment a saturation counter / log line each time the guard fires, so real-world trigger rate is measurable post-ship.

Why recursive-fold over mechanical Timeline truncation: no dependence on the model numbering the Timeline cleanly, no brittle list-parsing, and the fold preserves invariants by instruction. Bounded (single retry) so no runaway cost.

### 3.4 Parameters

| Name | Value | Notes |
|------|-------|-------|
| Soft summary target | ~16K tokens | Prompt guidance; steady-state operating point |
| Hard ceiling (`max_tokens`) | 20000 | Was 16000; both summarize paths |
| Guard retries | 1 | Recursive condense pass |
| New env var | none (decided) | Soft target **hardcoded 16K** first; promote to `ROLLING_CONTEXT_SUMMARY_TARGET` only if the saturation harness shows tuning is needed |

## 4. Data flow (unchanged pipeline)

```
trigger (100K real usage)
  → _find_keep_index (blend keep policy, unchanged)
  → _summarize_native  (clone session request → prompt-cache hit)
       → [NEW] capture stop_reason from message_delta
       → [NEW] guard: if truncated or over-ceiling → 1 recursive condense pass
  → summary_message = [SUMMARY_MARKER + summary + END_MARKER, ack] + verbatim tail
```

Prompt-cache: the summary prefix still changes only once per compression, and is now *smaller and stable* — neutral-to-favorable for cache hit rate. Emission shape (compressor.py:634-644) unchanged.

## 5. Testing

- **Unit — contract:** the three rewritten prompts contain the oldest-first-decay instruction and the invariant-preservation clause; no residual "never drop / copy verbatim" wording.
- **Unit — guard:** with a fake summarizer returning `stop_reason=max_tokens`, the recursive condense pass is invoked exactly once; with an over-ceiling summary, same; invariants (Active Goal / Key Details) survive the fold. Deterministic, no network.
- **Unit — stop_reason parse:** native SSE `message_delta` branch extracts `stop_reason` correctly from a canned stream.
- **Regression:** existing keep-tail and summary-emission tests remain green.
- **Saturation harness:** replay long real sessions through repeated compressions; assert summary token size **stabilizes at/below the ceiling** and does not grow monotonically with turn count (the test that would have caught the original bug).

## 6. Rollout

- Behavioral change to the shipped summarizer prompt → consequential; land behind review, verify the saturation harness before merge.
- No config migration; defaults change (cap 16000→20000). Note in README.
- Post-ship: watch the saturation counter to confirm real trigger rate matches the modeled ~1-in-4 estimate and that the guard rarely fires (decay should keep steady-state under soft target).

## 7. SOTA grounding

Design is the **fixed-budget tiered-decay delta summarization** pattern — independently converged on by web research and agy (Gemini 3.6) as the only feasible fit for a stateless zero-dependency proxy. Lineage:

- Reflexion — verbal self-updating memory: https://arxiv.org/abs/2303.11366
- Generative Agents — memory stream with decay/salience: https://arxiv.org/abs/2304.03442
- Recursively Summarizing Books (recursive rollup): https://arxiv.org/abs/2109.10862
- Chain of Density (dense abstraction under fixed budget): https://arxiv.org/abs/2309.04269

The stateful lane (MemGPT https://arxiv.org/abs/2310.08560, RAG, LLMLingua) is documented-infeasible from a proxy. LangChain `ConversationSummaryBufferMemory` is the cautionary precedent — it bounds the buffer but never hard-caps the running summary, which is the same class of bug this spec fixes.

*(All URLs to be re-verified at implementation time per the accuracy gate; a couple of arXiv ids surfaced by agy — MemDecay, Adaptive Context Compression — were not vouched for and are excluded.)*

## 8. Resolved decisions (2026-07-25)

- **Guard scope:** native path first; flattened-path guard is a fast-follow. Prompt-contract changes (§3.2) land on both paths day one.
- **Soft target:** hardcode 16K first; promote to `ROLLING_CONTEXT_SUMMARY_TARGET` only if the saturation harness shows steady-state doesn't land below the ceiling.
- To confirm on the harness before shipping: steady-state summary size stabilizes at/below the 16K soft target across repeated compressions.
