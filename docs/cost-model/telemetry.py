#!/usr/bin/env python3
"""Extract real token telemetry from Claude Code transcripts -> model params."""
import os, json, glob, statistics as st
from collections import defaultdict

files = glob.glob(os.path.expanduser("~/.claude/projects/**/*.jsonl"), recursive=True)
growths, outs, ctxs, turns_per, maxctx_per = [], [], [], [], []
cold_turns = tot_turns = 0
compactions = 0

for f in files:
    prev_ctx = None; sess_turns = 0; sess_max = 0
    try:
        with open(f, errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line: continue
                try: o = json.loads(line)
                except: continue
                msg = o.get("message") or {}
                u = msg.get("usage")
                if not isinstance(u, dict): continue
                it  = u.get("input_tokens", 0) or 0
                cc  = u.get("cache_creation_input_tokens", 0) or 0
                cr  = u.get("cache_read_input_tokens", 0) or 0
                ot  = u.get("output_tokens", 0) or 0
                ctx = it + cc + cr                      # total input the API saw this turn
                if ctx <= 0: continue
                tot_turns += 1; sess_turns += 1
                outs.append(ot); ctxs.append(ctx); sess_max = max(sess_max, ctx)
                # cold = big context but ~no cache read (cache miss / fresh)
                if ctx > 20000 and cr < 0.15*ctx: cold_turns += 1
                if prev_ctx is not None:
                    d = ctx - prev_ctx
                    if d > 0: growths.append(d)
                    elif d < -30000: compactions += 1   # context dropped a lot = compaction/reset
                prev_ctx = ctx
    except: continue
    if sess_turns >= 3:
        turns_per.append(sess_turns); maxctx_per.append(sess_max)

def pct(a,p): a=sorted(a); return a[min(len(a)-1,int(p/100*len(a)))] if a else 0
def med(a): return int(st.median(a)) if a else 0

print(f"sessions parsed (>=3 turns): {len(turns_per)}   total turns: {tot_turns}")
print(f"-- growth/turn (positive deltas): median={med(growths)}  p25={pct(growths,25)}  p75={pct(growths,75)}  p90={pct(growths,90)}")
print(f"-- output/turn:  median={med(outs)}  p75={pct(outs,75)}  p90={pct(outs,90)}")
print(f"-- context size: median={med(ctxs)}  p75={pct(ctxs,75)}  p90={pct(ctxs,90)}  p99={pct(ctxs,99)}")
print(f"-- turns/session: median={med(turns_per)}  p75={pct(turns_per,75)}  p90={pct(turns_per,90)}  max={max(turns_per) if turns_per else 0}")
print(f"-- max-ctx/session: median={med(maxctx_per)}  p90={pct(maxctx_per,90)}")
print(f"-- cold-turn fraction (ctx>20k, cache_read<15%): {cold_turns/max(1,tot_turns):.3f}")
print(f"-- compaction-like drops: {compactions}")
# emit params for model
print("\nMODELPARAMS", json.dumps(dict(
    G=med(growths), OUT=med(outs), TURNS=med([t for t in turns_per if t>=5] or [50]),
    P_COLD=round(cold_turns/max(1,tot_turns),3),
    MAXCTX_med=med(maxctx_per), MAXCTX_p90=pct(maxctx_per,90))))
