#!/usr/bin/env python3
"""Matched-trigger comparison: proxy vs native /compact at the SAME trigger.

Native's auto-compact threshold is user-configurable, so the fair comparison is at
EQUAL aggressiveness. At every matched trigger the proxy costs more, because it does
native's work (summarize the dropped span, pay cache invalidation) PLUS carries a
verbatim tail PLUS compacts a little more often. The premium shrinks as the trigger
rises (the fixed tail is a smaller slice of a bigger prefix) but never reaches zero.

Reads sessions.json (build it first with parse_sessions.py). Opus 4.8 flat pricing.
"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from fullmech import run

W = 1_000_000
print(f"{'trigger':>8} {'%1M':>5} {'native $':>10} {'proxy $':>10} {'proxy premium':>14}")
for T in (60_000, 80_000, 100_000, 129_000, 160_000, 200_000, 300_000):
    n = run(T, replace_all=True)["cost"]
    p = run(T)["cost"]
    print(f"{T//1000:>6}k {100*T/W:>4.1f}% ${n:>9,.0f} ${p:>9,.0f} {100*(p-n)/n:>+12.1f}%")
print("\nAt every matched trigger native is cheaper. The proxy's premium is the "
      "verbatim tail; it cannot be configured away, because native's trigger is a knob too.")
