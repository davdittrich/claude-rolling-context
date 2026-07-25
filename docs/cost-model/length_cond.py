#!/usr/bin/env python3
"""Session-length reality: survival, conditional expected remaining, and
at-context conditioning ('at context c now, how much more is coming?').
Uses reconstructed cumulative arrival = first + cumsum(positive growths)."""
import os, json, statistics as st

S = json.load(open(os.path.join(os.path.dirname(__file__), "sessions.json")))
N = len(S)
lengths = sorted(s["n_turns"] for s in S)
totarr = [s["first"] + sum(s["growths"]) for s in S]   # total content that arrives

def pct(a, p):
    a = sorted(a); return a[min(len(a) - 1, int(p / 100 * len(a)))] if a else 0

print(f"== {N} sessions, {sum(lengths)} turns ==")
print(f"turns/session: median={int(st.median(lengths))} mean={st.mean(lengths):.0f} "
      f"p75={pct(lengths,75)} p90={pct(lengths,90)} p95={pct(lengths,95)} p99={pct(lengths,99)} max={max(lengths)}")
print(f"total arrival/session (tok): median={int(st.median(totarr)):,} "
      f"p75={pct(totarr,75):,} p90={pct(totarr,90):,} p99={pct(totarr,99):,} max={max(totarr):,}")

# ---- A. length survival + conditional expected REMAINING turns ----
print("\n== A. length survival S(n)=P(L>=n) and E[remaining turns | L>=n] ==")
print(f"{'turn n':>7} {'#sess>=n':>9} {'S(n)':>7} {'E[rem|>=n]':>11} {'E[total|>=n]':>13}")
for n in (1, 5, 10, 20, 28, 40, 60, 88, 120, 200, 400, 800, 1600):
    surv = [L for L in lengths if L >= n]
    if not surv:
        continue
    e_rem = st.mean(L - n for L in surv)
    print(f"{n:>7} {len(surv):>9} {len(surv)/N:>7.3f} {e_rem:>11.1f} {st.mean(surv):>13.1f}")

# ---- B. who ever crosses trigger T (total arrival > T) ----
print("\n== B. fraction of sessions whose total arrival exceeds trigger T ==")
print(f"{'T':>8} {'frac>T':>8} {'#sess':>7}")
for T in (40_000, 60_000, 80_000, 100_000, 120_000, 140_000, 160_000, 200_000, 300_000):
    cnt = sum(1 for a in totarr if a > T)
    print(f"{T//1000:>6}k {cnt/N:>8.3f} {cnt:>7}")

# ---- C. at-context conditioning: reach cumulative c -> remaining turns & arrival ----
# For every turn of every session, record (cumulative_ctx, remaining_turns, remaining_arrival).
bins = [40, 60, 80, 100, 120, 140, 160, 200, 300, 500]  # thousands
acc = {b: {"rem_turns": [], "rem_arr": [], "will_grow_50k": 0, "n": 0} for b in bins}
for s in S:
    g = s["growths"]; c = s["first"]; T = len(g)
    cum = [c]
    for x in g:
        c += x; cum.append(c)
    total = cum[-1]
    for t in range(len(cum)):
        cval = cum[t]
        rem_turns = (len(cum) - 1) - t
        rem_arr = total - cval
        # assign to the highest bin threshold <= cval (i.e., 'having reached at least b')
        for b in bins:
            if cval >= b * 1000:
                d = acc[b]
                d["n"] += 1; d["rem_turns"].append(rem_turns); d["rem_arr"].append(rem_arr)
                if rem_arr >= 50_000:
                    d["will_grow_50k"] += 1
print("\n== C. conditional on HAVING REACHED cumulative context >= c (turn-weighted) ==")
print(f"{'c':>7} {'#turns>=c':>10} {'E[rem turns]':>12} {'med rem':>8} {'E[rem arrival]':>15} {'P(>=50k more)':>14}")
for b in bins:
    d = acc[b]
    if d["n"] == 0:
        continue
    print(f"{b:>5}k {d['n']:>10} {st.mean(d['rem_turns']):>12.1f} "
          f"{int(st.median(d['rem_turns'])):>8} {st.mean(d['rem_arr']):>15,.0f} "
          f"{d['will_grow_50k']/d['n']:>14.3f}")
