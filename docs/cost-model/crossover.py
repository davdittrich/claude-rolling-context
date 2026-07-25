#!/usr/bin/env python3
"""Proxy pinned at 100K vs native at a SWEPT trigger (100K -> 900K).

Native's per-turn cost rises with its trigger (a bigger prefix carried at 0.1x every
turn), so once native's trigger climbs past ~190K the fixed-100K proxy becomes cheaper
-- up to ~50% cheaper at a near-limit 900K native trigger. Two honest caveats:

  1. This is the ASYMMETRIC comparison. At a MATCHED trigger (matched.py) native is
     cheaper at every setting. The proxy only wins here by compacting EARLIER, and
     native's trigger is a knob, so this is not a fair tool-vs-tool edge.
  2. Every dollar here is a CORPUS TOTAL over 1,940 sessions, and it lives in the
     extreme tail: only ~1% of sessions ever reach 900K, ~10 sessions are ~40% of the
     900K total, and the MEDIAN session costs ~$1 under any trigger. For typical use
     (never near the window) native-high vs proxy-100K is a wash; the ~50% gap only
     appears if you actually run multi-thousand-turn, near-limit sessions.

Reads sessions.json (build it first with parse_sessions.py). Opus 4.8 flat pricing.
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from fullmech import run

W = 1_000_000
proxy = run(100_000)["cost"]
print(f"proxy FIXED @100K (current params) = ${proxy:,.0f}  [corpus total, 1,940 sessions]\n")
print(f"{'native trig':>11} {'%1M':>5} {'native $':>10} {'proxy vs native':>16}")
for T in range(100_000, 900_001, 100_000):
    c = run(T, replace_all=True)["cost"]
    rel = 100 * (proxy - c) / c
    print(f"{T//1000:>9}k {100*T/W:>4.0f}% ${c:>9,.0f} {rel:>+9.1f}%  "
          f"{'proxy cheaper' if proxy < c else 'native cheaper'}")

lo, hi = 100_000, 950_000
for _ in range(40):
    mid = (lo + hi) // 2
    if run(mid, replace_all=True)["cost"] < proxy:
        lo = mid
    else:
        hi = mid
print(f"\nasymmetric break-even: native trigger = {hi/1000:.0f}K = {100*hi/W:.1f}% of the 1M window")
print("above it the (early-compacting) proxy is cheaper; below it native is.")
print("BUT at a matched trigger native is always cheaper (matched.py), and the gap")
print("above lives almost entirely in the <=1%-of-sessions extreme tail.")
