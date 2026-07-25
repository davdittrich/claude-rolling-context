# Cost model — reproducing the numbers in the design brief

Every figure in [`../design-brief.md`](../design-brief.md) §4–§5 comes from one of the
scripts here. They are the working artifacts, committed so the claims are auditable
rather than asserted. Nothing here runs in production — the proxy has no dependency on
this directory.

## What each script is

| Script | Answers | Data source |
|---|---|---|
| `telemetry.py` | What do real sessions look like? (growth/turn, context-size, turns/session percentiles) | Your own `~/.claude/projects/**/*.jsonl` |
| `keepfloor.py` | How big is a single turn, and a rolling N-turn working set? (justifies the keep floor) | Your own `~/.claude/projects` |
| `final.py` | Where is the trigger/keep cost optimum? (basin sweep, first-principles model) | Self-contained (telemetry baked in as params) |
| `replay.py` | Blend keep vs. flat-token keep, on real growth traces (cost + coverage + coherence) | Your own `~/.claude/projects` |
| `parse_sessions.py` | Builds the shared `sessions.json` cache (per-session growth traces) the full-mechanics scripts read | Your own `~/.claude/projects/**/*.jsonl` |
| `fullmech.py` | Full-mechanics trigger sweep → the 100K optimum & basin (blend keep, saturating summary, decay guard, cache-invalidation) | `sessions.json` |
| `setpoint.py` | Where does native `/compact` actually fire? (median context before real compaction drops) | Your own `~/.claude/projects/**/*.jsonl` |
| `summ_rate.py` | Proxy vs. native `/compact`, and the cheaper-summarizer (Haiku/Sonnet) lever | `sessions.json` |
| `length_cond.py` | Session-length distribution & conditional-remaining (why 100K, not lower) | `sessions.json` |
| `adaptive.py` | Do turn/velocity-gated triggers beat a fixed 100K? (no, materially) | `sessions.json` |
| `think_sens.py` | How do extended-thinking tokens move the savings? | Self-contained |
| `profile_hash.py` | Is the proxy's own overhead material? (hash + TLS timing) | Live timing, imports `../../proxy/server.py` |

`final.py` and `think_sens.py` are pure models — run them anywhere. The others read
**your** local Claude Code transcripts (directly, or via the `sessions.json` cache that
`parse_sessions.py` builds) and emit only aggregate statistics — percentiles, counts,
costs. No transcript content is stored or committed; `sessions.json` holds only per-turn
token *counts* and is git-ignored. The paths are `~`-relative, so they profile whoever runs them.

## Pricing (the units the model runs in)

All scripts share one pricing block — Anthropic's published Opus 4.8 rates, which price the
full 1M context window **flat** ($5 / $25 per MTok input/output, as of July 2026):

```
INP, CW, CR, OUT = 5e-6, 6.25e-6, 0.50e-6, 25e-6
```

Fresh input `1×`, cache **write** `1.25×`, cache **read** `0.1×`, output `5×`. There is **no
>200K long-context premium** for Opus 4.8: the `2× input / 1.5× output` tier above 200K was a
Sonnet-4/4.5-era feature, retired with the 4.6 generation. See Anthropic's
[pricing](https://platform.claude.com/docs/en/about-claude/pricing) and
[prompt-caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) docs;
update this block if rates change.

## Telemetry snapshot (this author's corpus, for calibration)

`telemetry.py` + `keepfloor.py` over ~1,900 sessions / ~112K turns produced the parameters
baked into the models:

- growth/turn: median **1.6K**, p90 **6.6K**
- context size: median **89K**, p90 **487K**, p99 **878K**
- turns/session: median **28**, p90 **88**
- single-turn size: median **1.6K**, p95 **11K**, p99 **67K**, max **1.47M** (one giant dump)
- cold-turn fraction (context >20K, cache-read <15%): **≈0.08**

Your corpus will differ; re-run the scripts to recalibrate.

## Headline results, and where they land in the brief

- **Trigger optimum** (`fullmech.py`, full mechanics over 1,940 sessions; `final.py` is the
  earlier simplified model): flat-bottomed basin, optimum at **exactly 100K**, within 1% over
  90K–120K; a 50K trigger costs ~36% more, 200K ~11% more. → brief §4.
- **Blend keep beats flat-token** (`replay.py`, ~430 crossing sessions): the shipped
  `N=8 / hi=40K / floor=3` costs **$1,231 vs. $1,407** for flat `token-40k` — **−12.5%** at
  equal 5-turn coverage (99.3%), and it removes the flat policy's 15.5% "kept only one giant
  turn" coherence hazard (→ 0%). → brief §3, §4.
- **Proxy vs. native `/compact` — not a cost saving** (`summ_rate.py` + `setpoint.py`): native
  compaction fires at a **median 129K** in real transcripts (658 drops measured). Against it the
  proxy is **~6–15% *more* expensive**, not cheaper — it keeps a verbatim tail and compacts more
  often. The cheaper-summarizer lever (Haiku) would flip that on token billing, but is blocked on
  subscription OAuth. The old "~12% cheaper than native" figure came from a mislabeled baseline
  (a 40K-tail policy, not true native) and is retracted. → brief §5.
- **Session length** (`length_cond.py`, `adaptive.py`): only ~28% of sessions ever reach 100K;
  the amortization break-even (~70–100 turns) ≈ median remaining at 100K (~74 turns), which pins
  the optimum there; turn/velocity-gated triggers add < 0.1%. → brief §4.
- **Overhead is negligible** (`profile_hash.py`): hashing the message array is **~0.8 ms** at
  the operating point; TLS handshake **~40 ms** — both dwarfed by the 2–15 s LLM stream. → brief §5.

## Caveats

- This is a **model calibrated to real usage**, not a live billing A/B. Treat figures as
  grounded estimates, not invoices.
- Output is modeled at a flat per-turn token count (telemetry median ≈ light thinking).
  Extended-thinking tokens are compression-invariant; `think_sens.py` shows they leave the
  dollar saving flat-or-higher while shrinking the *percentage*. See brief §5.
- Deterministic by construction (cold turns spread via a fixed hash, no RNG), so runs are
  reproducible.
