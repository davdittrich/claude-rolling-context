#!/usr/bin/env python3
"""Does conditioning the trigger on session-length-so-far beat a fixed trigger?
Heavy-tail hypothesis: reaching a high context is ITSELF the signal of a long
session, so a fixed context trigger already captures most of the amortization
information. Test turn-conditional + velocity-conditional triggers vs fixed."""
import os, json, statistics as st

S = json.load(open(os.path.join(os.path.dirname(__file__), "sessions.json")))
INP, CW, CR, OUT = 5e-6, 6.25e-6, 0.50e-6, 25e-6
tr_read = lambda t: t * CR
tr_write = lambda t: t * CW
P_COLD = 0.08; OUT_TOK = 329; MIN_TURN = 6
KEEP_N, KEEP_HI, KEEP_FLOOR = 8, 40_000, 3
RHO, SOFT, HARD = 0.035, 16_000, 20_000

def keep_blend(win):
    k = 0; s = 0
    for g in reversed(win):
        if k >= KEEP_N: break
        if k >= KEEP_FLOOR and s + g > KEEP_HI: break
        s += g; k += 1
    return max(min(KEEP_FLOOR, len(win)), k)

def cold(t):
    return (t * 2654435761 % 1000) / 1000.0 < P_COLD

def sim(growths, first, trig):
    """trig(t, C, win) -> threshold (tokens). Compaction when C > threshold."""
    window = [first]; summ = 0; cost = 0.0; last = first; uncached = False; nc = 0
    for t, g in enumerate(growths):
        C = summ + sum(window)
        cost += tr_write(C) if (uncached or cold(t)) else tr_write(last) + tr_read(max(0, C - last))
        uncached = False; cost += OUT_TOK * OUT
        window.append(g); last = g
        C = summ + sum(window)
        thr = trig(t, C, window)
        if thr and C > thr and t >= MIN_TURN:
            k = keep_blend(window); dropped = sum(window[:len(window) - k])
            if dropped <= 0: continue
            S_nat = summ + RHO * dropped; emit = min(HARD, S_nat)
            cost += tr_read(dropped) + emit * OUT
            if emit > SOFT:
                cost += tr_read(emit) + SOFT * OUT; summ = SOFT
            else:
                summ = emit
            window = window[len(window) - k:]; nc += 1; uncached = True
    return cost, nc

def total(trig):
    c = 0.0; nc = 0
    for s in S:
        cc, n = sim(s["growths"], s["first"], trig); c += cc; nc += n
    return c, nc

# --- policies ---
fixed = lambda T: (lambda t, C, w: T)
# turn-gated: high trigger early (avoid compacting sessions that may end), lower once proven long
def turn_gated(hi, lo, k):
    return lambda t, C, w: hi if t < k else lo
# velocity-gated: if recent turns are big (burst/dump), raise trigger (burst may end soon)
def vel_gated(base, hi, big):
    def f(t, C, w):
        recent = sum(w[-3:]) / min(3, len(w))
        return hi if recent > big else base
    return f

print("== fixed triggers (ref) ==")
for T in (80, 90, 100, 110, 120):
    c, nc = total(fixed(T * 1000))
    print(f"  fixed {T}k: ${c:,.0f}  comps={nc}")

base_c, _ = total(fixed(100_000))
print(f"\n== turn-gated (hi early, lo after k turns) vs fixed 100k (${base_c:,.0f}) ==")
for hi, lo, k in [(130,90,20),(140,90,30),(120,90,15),(150,80,30),(130,95,25),(160,90,40)]:
    c, nc = total(turn_gated(hi*1000, lo*1000, k))
    print(f"  hi={hi}k lo={lo}k after t>={k}: ${c:,.0f} ({100*(c-base_c)/base_c:+.2f}%)  comps={nc}")

print(f"\n== velocity-gated vs fixed 100k ==")
for base, hi, big in [(100,140,8000),(100,150,6000),(90,140,7000),(100,130,10000)]:
    c, nc = total(vel_gated(base*1000, hi*1000, big))
    print(f"  base={base}k hi={hi}k big>{big}: ${c:,.0f} ({100*(c-base_c)/base_c:+.2f}%)  comps={nc}")

# --- amortization break-even, empirical ---
print("\n== amortization: marginal overhead vs per-turn saving @100k ==")
# measure by finite difference: cost with vs without one extra compaction band is noisy;
# instead report modeled marginal OH and the sweep-implied saving.
OH_noguard = tr_read(44_000) + 16_000 * OUT
OH_guard = OH_noguard + tr_read(16_000) + SOFT * OUT
recache = 56_000 * (CW - CR)
print(f"  marginal compaction overhead: no-guard ${OH_noguard:.2f}, guard ${OH_guard:.2f}, +re-cache ${recache:.2f}")
print(f"  => OH ~= ${OH_noguard+recache:.2f} (typical) .. ${OH_guard+recache:.2f} (saturated)")
sav = 22_000 * CR
print(f"  per-turn saving ~ (T-keep-S)/2 * CR = ${sav:.4f}/turn")
print(f"  break-even ~ {int((OH_noguard+recache)/sav)}..{int((OH_guard+recache)/sav)} turns after the compaction")
