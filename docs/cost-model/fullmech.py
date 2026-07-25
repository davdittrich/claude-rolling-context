#!/usr/bin/env python3
"""Full-mechanics cost model of the CURRENT proxy implementation, replayed over
1940 real Claude Code session arrival streams. Opus 4.8 flat 1M pricing.

Mechanics modeled (vs the old flat-summary=3000 model):
  - Blend keep policy: keep last N=8 turns, up to hi=40k tokens, floor=3 turns.
  - Rolling summary that GROWS with cumulative dropped content and SATURATES:
        S_natural = prev_summary + rho * dropped_this_compaction
        emit      = min(HARD, S_natural)            # what the model actually outputs
        guard fires if emit > SOFT  -> one condense pass -> new summary = SOFT
        else new summary = emit
  - Native summarize cost = cache-read of the dropped span + output of `emit`
    (+ condense pass when the guard fires).
  - Compaction invalidates the prompt cache -> next turn re-writes the new prefix.
  - Per-turn warm cost = cache-write(new delta) + cache-read(rest); cold turns re-write all.
Summary-size params (rho, SOFT) are ASSUMPTIONS (never measured) -> swept for sensitivity.
"""
import os, json, statistics as st

S = json.load(open(os.path.join(os.path.dirname(__file__), "sessions.json")))

# Opus 4.8: $5/$25 per MTok; 5m cache-write 1.25x; cache-read 0.1x. (verified Jul 2026)
INP, CW, CR, OUT = 5e-6, 6.25e-6, 0.50e-6, 25e-6
tr_read = lambda t: t * CR
tr_write = lambda t: t * CW

P_COLD = 0.08
OUT_TOK = 329            # median task output/turn (trigger-independent constant)
MIN_TURN = 6
KEEP_N, KEEP_HI, KEEP_FLOOR = 8, 40_000, 3
WINDOW_LIMIT = 1_000_000  # 1M context window; never-compact caps here (infeasible beyond)

def keep_blend(win, N=KEEP_N, hi=KEEP_HI, floor=KEEP_FLOOR):
    k = 0; s = 0
    for g in reversed(win):
        if k >= N: break
        if k >= floor and s + g > hi: break
        s += g; k += 1
    return max(min(floor, len(win)), k)

def cold(t):
    return (t * 2654435761 % 1000) / 1000.0 < P_COLD   # deterministic, non-phase-locked

def sim_session(growths, first, trigger, rho, SOFT, HARD, replace_all=False):
    """Return (cost, n_comp, n_guard, mean_C, peak_C). replace_all=native-compact (keep=0, summary only)."""
    window = [first]           # kept verbatim turns (first msg seeds it)
    summ = 0; cumdrop = 0
    cost = 0.0; last = first; uncached = False
    n_comp = n_guard = 0; Csum = 0.0; peak = 0; nt = 0
    for t, g in enumerate(growths):
        C = summ + sum(window)
        # per-turn serve cost
        if uncached or cold(t):
            cost += tr_write(C)
        else:
            cost += tr_write(last) + tr_read(max(0, C - last))
        uncached = False
        cost += OUT_TOK * OUT
        window.append(g); last = g
        C = summ + sum(window)
        Csum += C; peak = max(peak, C); nt += 1
        if trigger and C > trigger and t >= MIN_TURN:
            if replace_all:
                dropped = sum(window)            # native: replace everything
                k = 0
            else:
                k = keep_blend(window)
                dropped = sum(window[:len(window) - k])
            if dropped <= 0:
                continue
            cumdrop += dropped
            S_natural = summ + rho * dropped
            emit = min(HARD, S_natural)
            cost += tr_read(dropped)             # summarize input (native cache-read of span)
            cost += emit * OUT                   # summarize output
            if emit > SOFT:                      # guard: one condense pass -> SOFT
                n_guard += 1
                cost += tr_read(emit) + SOFT * OUT
                summ = SOFT
            else:
                summ = emit
            window = window[len(window) - k:] if k > 0 else []
            n_comp += 1
            uncached = True
        elif trigger is None and C > WINDOW_LIMIT:
            pass  # infeasible; cost keeps accruing at capped size (flag separately)
    return cost, n_comp, n_guard, (Csum / max(1, nt)), peak

def run(trigger, rho=0.035, SOFT=16_000, HARD=20_000, replace_all=False):
    tot = 0.0; comps = 0; guards = 0; meanCs = []; peaks = []
    for s in S:
        c, nc, ng, mC, pk = sim_session(s["growths"], s["first"], trigger, rho, SOFT, HARD, replace_all)
        tot += c; comps += nc; guards += ng; meanCs.append(mC); peaks.append(pk)
    return dict(cost=tot, comps=comps, guards=guards,
                meanC=st.mean(meanCs), peak=max(peaks))

if __name__ == "__main__":
    print("== FULL-MECHANICS trigger sweep (rho=0.035, SOFT=16k, HARD=20k) ==")
    base_never = run(None)
    print(f"never-compact (capped@1M, INFEASIBLE ref): ${base_never['cost']:,.0f}  meanC={base_never['meanC']:,.0f}")
    print(f"{'trigger':>8} {'cost':>12} {'vs opt':>7} {'comps':>7} {'guards':>7} {'meanC':>9} {'$/comp':>7}")
    grid = list(range(50_000, 260_001, 10_000))
    res = [(T, run(T)) for T in grid]
    opt = min(res, key=lambda x: x[1]["cost"])
    for T, r in res:
        dpc = (r["cost"]) / max(1, r["comps"])
        mark = "  <== OPT" if T == opt[0] else ""
        print(f"{T//1000:>6}k ${r['cost']:>11,.0f} {100*(r['cost']-opt[1]['cost'])/opt[1]['cost']:>6.1f}% "
              f"{r['comps']:>7} {r['guards']:>7} {r['meanC']:>9,.0f} ${dpc:>6.2f}{mark}")
    print(f"\nOPTIMUM trigger = {opt[0]//1000}k  (${opt[1]['cost']:,.0f})")
    # basin: within 1%
    basin = [T for T, r in res if r["cost"] <= opt[1]["cost"] * 1.01]
    print(f"basin within 1% of opt: {min(basin)//1000}k .. {max(basin)//1000}k")
    basin2 = [T for T, r in res if r["cost"] <= opt[1]["cost"] * 1.02]
    print(f"basin within 2% of opt: {min(basin2)//1000}k .. {max(basin2)//1000}k")

    print("\n== native-compact baseline (replace-all, keep=0) at same triggers ==")
    for T in (100_000, 160_000, 200_000):
        r = run(T, replace_all=True)
        print(f"  native @ {T//1000}k: ${r['cost']:,.0f}  comps={r['comps']}  meanC={r['meanC']:,.0f}")
    r100 = run(100_000)
    rn160 = run(160_000, replace_all=True)
    print(f"\nproxy@opt vs native@160k: proxy${opt[1]['cost']:,.0f} vs native${rn160['cost']:,.0f} "
          f"-> {100*(opt[1]['cost']-rn160['cost'])/rn160['cost']:+.1f}%")

    print("\n== SENSITIVITY: optimum vs summary-size assumptions ==")
    print(f"{'rho':>6} {'SOFT':>6} {'opt trigger':>12} {'opt cost':>12}")
    for rho in (0.02, 0.035, 0.05, 0.08):
        for SOFT in (12_000, 16_000, 20_000):
            HARD = int(SOFT * 1.25)
            rr = [(T, run(T, rho=rho, SOFT=SOFT, HARD=HARD)["cost"]) for T in grid]
            o = min(rr, key=lambda x: x[1])
            print(f"{rho:>6.3f} {SOFT//1000:>4}k {o[0]//1000:>10}k ${o[1]:>11,.0f}")
