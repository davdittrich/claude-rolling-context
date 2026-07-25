#!/usr/bin/env python3
"""Break-even: proxy fixed at 100K vs native at a SWEPT trigger.

Native's per-turn cost rises with its trigger (a bigger prefix carried at 0.1x every
turn), so there is a crossover where native gets more expensive than the fixed-100K
proxy. But that crossover only exists because the proxy compacts EARLIER than native.
It is not a fair comparison: native's trigger is configurable, so at a MATCHED trigger
(see matched.py) native is cheaper at every setting. This script quantifies the
asymmetric crossover only to show where it sits (~19% of a 1M window).

Reads sessions.json (build it first with parse_sessions.py). Opus 4.8 flat pricing.
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from fullmech import run

W = 1_000_000
proxy = run(100_000)["cost"]
print(f"proxy @100K (current params) = ${proxy:,.0f}  [fixed reference]\n")
print(f"{'native trig':>11} {'%of1M':>6} {'native $':>10} {'cheaper':>10}")
for T in (129_000, 160_000, 190_000, 200_000, 300_000, 920_000):
    c = run(T, replace_all=True)["cost"]
    print(f"{T//1000:>9}k {100*T/W:>5.1f}% ${c:>9,.0f} {('proxy' if proxy < c else 'native'):>10}")

lo, hi = 100_000, 950_000
for _ in range(40):
    mid = (lo + hi) // 2
    if run(mid, replace_all=True)["cost"] < proxy:
        lo = mid
    else:
        hi = mid
print(f"\nasymmetric break-even: native trigger = {hi/1000:.0f}K = {100*hi/W:.1f}% of the 1M window")
print("above it the (early-compacting) proxy is cheaper; below it native is.")
print("BUT at a matched trigger native is always cheaper -- see matched.py.")
