#!/usr/bin/env python3
"""Can the proxy be cost-neutral-or-better than native compact WHILE keeping a
verbatim tail? Only lever: summarizer model rate (native /compact is locked to
Opus; proxy can use Haiku/Sonnet via ROLLING_CONTEXT_MODEL -> flattened).

Key modeling fact: which model produces the SUMMARY changes only the per-compaction
overhead. The MAIN per-turn serve cost is Opus-with-cache in every variant (the
rewritten summary+tail prefix caches identically regardless of summarizer). So
proxy-Haiku vs proxy-Opus differ ONLY in compaction in/out cost."""
import os, json, statistics as st

S = json.load(open(os.path.join(os.path.dirname(__file__), "sessions.json")))
CW, CR, OUT = 6.25e-6, 0.50e-6, 25e-6      # Opus main-model cache-write/read, output
tr_read = lambda t: t * CR
tr_write = lambda t: t * CW
P_COLD = 0.08; OUT_TOK = 329; MIN_TURN = 6
RHO, SOFT, HARD = 0.035, 16_000, 20_000

def keep_blend(win, N, hi, floor):
    k = 0; s = 0
    for g in reversed(win):
        if k >= N: break
        if k >= floor and s + g > hi: break
        s += g; k += 1
    return max(min(floor, len(win)), k)

def cold(t):
    return (t * 2654435761 % 1000) / 1000.0 < P_COLD

def sim(growths, first, trigger, summ_in, summ_out, replace_all,
        N=8, hi=40_000, floor=3):
    """summ_in/summ_out = per-token rates for the SUMMARIZER call (in-read, out-gen)."""
    window = [first]; summ = 0; cost = 0.0; last = first; uncached = False
    nc = 0; keptsum = 0.0; Csum = 0.0; nt = 0
    for t, g in enumerate(growths):
        C = summ + sum(window)
        cost += tr_write(C) if (uncached or cold(t)) else tr_write(last) + tr_read(max(0, C - last))
        uncached = False; cost += OUT_TOK * OUT
        window.append(g); last = g
        C = summ + sum(window); Csum += C; nt += 1
        if trigger and C > trigger and t >= MIN_TURN:
            if replace_all:
                dropped = sum(window); k = 0
            else:
                k = keep_blend(window, N, hi, floor); dropped = sum(window[:len(window) - k])
            if dropped <= 0:
                continue
            S_nat = summ + RHO * dropped; emit = min(HARD, S_nat)
            cost += dropped * summ_in + emit * summ_out          # summarizer in + out
            if emit > SOFT:
                cost += emit * summ_in + SOFT * summ_out         # condense pass
                summ = SOFT
            else:
                summ = emit
            keptsum += (sum(window) - dropped)                   # verbatim tail retained
            window = window[len(window) - k:] if k > 0 else []
            nc += 1; uncached = True
    return cost, nc, (keptsum / max(1, nc)), (Csum / max(1, nt))

def run(trigger, summ_in, summ_out, replace_all=False, N=8, hi=40_000, floor=3):
    c = 0.0; nc = 0; keeps = []; Cs = []
    for s in S:
        cc, n, kp, mC = sim(s["growths"], s["first"], trigger, summ_in, summ_out, replace_all, N, hi, floor)
        c += cc; nc += n
        if n: keeps.append(kp)
        Cs.append(mC)
    return dict(cost=c, comps=nc, meankeep=(st.mean(keeps) if keeps else 0), meanC=st.mean(Cs))

# summarizer rate profiles: (in-read rate, out-gen rate)
OPUS_CACHE = (CR, OUT)        # native-mode: cache-read span, Opus output  (0.5/M, 25/M)
HAIKU_FRESH = (1e-6, 5e-6)    # flattened: fresh Haiku input, Haiku output
SONNET_FRESH = (3e-6, 15e-6)  # flattened: fresh Sonnet input, Sonnet output

print("== NATIVE COMPACT baseline (replace-all, Opus-locked summarizer) ==")
nat = {}
for T in (100_000, 130_000, 160_000):
    r = run(T, *OPUS_CACHE, replace_all=True)
    nat[T] = r["cost"]
    print(f"  native @ {T//1000}k : ${r['cost']:,.0f}  comps={r['comps']}  tail=0  meanC={r['meanC']:,.0f}")
NAT160 = nat[160_000]; NAT100 = nat[100_000]

print("\n== PROXY variants @ trigger 100k (keep blend N=8/40k/floor3) ==")
for label, rate in [("native-Opus summarizer (default)", OPUS_CACHE),
                    ("Sonnet summarizer", SONNET_FRESH),
                    ("Haiku summarizer", HAIKU_FRESH)]:
    r = run(100_000, *rate)
    vs160 = 100 * (r["cost"] - NAT160) / NAT160
    vs100 = 100 * (r["cost"] - NAT100) / NAT100
    print(f"  {label:34} ${r['cost']:,.0f}  tail={r['meankeep']:,.0f}  "
          f"vs native@160k {vs160:+.1f}%  vs native@100k {vs100:+.1f}%")

print("\n== PROXY-HAIKU trigger sweep vs native baselines ==")
print(f"{'trigger':>8} {'cost':>10} {'vs nat@160':>11} {'vs nat@100':>11} {'tail':>8} {'meanC':>9}")
for T in range(80_000, 200_001, 20_000):
    r = run(T, *HAIKU_FRESH)
    print(f"{T//1000:>6}k ${r['cost']:>9,.0f} {100*(r['cost']-NAT160)/NAT160:>10.1f}% "
          f"{100*(r['cost']-NAT100)/NAT100:>10.1f}% {r['meankeep']:>8,.0f} {r['meanC']:>9,.0f}")

print("\n== PROXY-HAIKU keep sweep @100k: max affordable verbatim tail at cost-neutral ==")
print(f"  native@160k=${NAT160:,.0f}  native@100k=${NAT100:,.0f}")
print(f"{'keep(N,hi)':>16} {'cost':>10} {'vs nat@160':>11} {'vs nat@100':>11} {'tail':>8}")
for N, hi, floor in [(4,20_000,2),(6,30_000,3),(8,40_000,3),(8,50_000,3),(12,60_000,3),(16,90_000,3)]:
    r = run(100_000, *HAIKU_FRESH, N=N, hi=hi, floor=floor)
    print(f"  N={N:>2},hi={hi//1000:>2}k ${r['cost']:>9,.0f} {100*(r['cost']-NAT160)/NAT160:>10.1f}% "
          f"{100*(r['cost']-NAT100)/NAT100:>10.1f}% {r['meankeep']:>8,.0f}")
