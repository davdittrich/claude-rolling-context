#!/usr/bin/env python3
"""Where does native compaction ACTUALLY fire? Measure the context size just
before each large mid-session drop (-30k..-200k) in real transcripts. That drop
is Claude Code's own auto-compact / manual /compact firing. This resolves the
pivotal question: is native's real setpoint >= ~150k (proxy-Haiku wins) or lower?"""
import os, json, glob, statistics as st

files = glob.glob(os.path.expanduser("~/.claude/projects/**/*.jsonl"), recursive=True)
pre_drop = []      # context just before a native compaction fired
drop_size = []
for f in files:
    ctxs = []
    try:
        with open(f, errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                u = (o.get("message") or {}).get("usage")
                if not isinstance(u, dict):
                    continue
                c = ((u.get("input_tokens", 0) or 0)
                     + (u.get("cache_creation_input_tokens", 0) or 0)
                     + (u.get("cache_read_input_tokens", 0) or 0))
                if c > 0:
                    ctxs.append(c)
    except Exception:
        continue
    for i in range(1, len(ctxs)):
        d = ctxs[i] - ctxs[i - 1]
        if -200_000 < d < -30_000:      # native compaction fired between i-1 and i
            pre_drop.append(ctxs[i - 1])
            drop_size.append(-d)

def pct(a, p):
    a = sorted(a); return a[min(len(a) - 1, int(p / 100 * len(a)))] if a else 0

print(f"native compactions detected: {len(pre_drop)}")
print(f"context at firing (pre-drop):")
print(f"  median={int(st.median(pre_drop)):,}  mean={int(st.mean(pre_drop)):,}")
print(f"  p25={pct(pre_drop,25):,}  p75={pct(pre_drop,75):,}  p90={pct(pre_drop,90):,}")
print(f"drop size: median={int(st.median(drop_size)):,}  p75={pct(drop_size,75):,}")
# fraction firing above key thresholds
for T in (100_000, 130_000, 150_000, 160_000, 180_000):
    frac = sum(1 for x in pre_drop if x >= T) / len(pre_drop)
    print(f"  fired at ctx >= {T//1000}k: {frac:.2f}")
