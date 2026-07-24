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
| `baseline_honesty.py` | Proxy vs. which baseline? (never-compact vs. native auto-compact) | Self-contained |
| `think_sens.py` | How do extended-thinking tokens move the savings? | Self-contained |
| `profile_hash.py` | Is the proxy's own overhead material? (hash + TLS timing) | Live timing, imports `../../proxy/server.py` |

`final.py`, `baseline_honesty.py`, and `think_sens.py` are pure models — run them
anywhere. The other three read **your** local Claude Code transcripts and emit only
aggregate statistics (percentiles, counts). No transcript content is stored or committed;
the paths are `~`-relative, so they profile whoever runs them.

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

- **Trigger basin** (`final.py`): flat-bottomed ~80K–115K; 100K within ~3% of the optimum
  (~112K) for long sessions; a 60K trigger costs ~22% more, and pushing past ~120K also costs
  (you carry too much). → brief §4.
- **Blend keep beats flat-token** (`replay.py`, ~430 crossing sessions): the shipped
  `N=8 / hi=40K / floor=3` costs **$1,231 vs. $1,407** for flat `token-40k` — **−12.5%** at
  equal 5-turn coverage (99.3%), and it removes the flat policy's 15.5% "kept only one giant
  turn" coherence hazard (→ 0%). → brief §3, §4.
- **Honest baseline** (`baseline_honesty.py`): vs. "never compact" the proxy saves ~63% on a
  long active session, but that baseline is a strawman nobody should run. Against Claude Code's
  *own* auto-compaction (~160K setpoint) the cost edge is **~12%** — and that ~12% is invariant
  to the pricing correction, because both sides keep the prefix well under any tier. → brief §5.
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
