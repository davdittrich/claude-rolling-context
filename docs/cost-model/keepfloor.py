#!/usr/bin/env python3
"""KEEP floor = quality constraint (verbatim working set), not cost min.
Pin from real single-turn growth tail + rolling N-turn cumulative growth."""
import os, json, glob, statistics as st

files = glob.glob(os.path.expanduser("~/.claude/projects/**/*.jsonl"), recursive=True)
growths=[]; roll3=[]; roll5=[]; roll8=[]; big_single=0
for f in files:
    ctxs=[]
    try:
        with open(f, errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line: continue
                try: o=json.loads(line)
                except: continue
                u=(o.get("message") or {}).get("usage")
                if not isinstance(u,dict): continue
                ctx=(u.get("input_tokens",0)or 0)+(u.get("cache_creation_input_tokens",0)or 0)+(u.get("cache_read_input_tokens",0)or 0)
                if ctx>0: ctxs.append(ctx)
    except: continue
    d=[ctxs[i]-ctxs[i-1] for i in range(1,len(ctxs)) if ctxs[i]-ctxs[i-1]>0]
    growths+=d
    for i in range(len(d)):
        roll3.append(sum(d[max(0,i-2):i+1]))
        roll5.append(sum(d[max(0,i-4):i+1]))
        roll8.append(sum(d[max(0,i-7):i+1]))

def pc(a,p): a=sorted(a); return a[min(len(a)-1,int(p/100*len(a)))] if a else 0
print(f"single-turn growth  p50={pc(growths,50)} p90={pc(growths,90)} p95={pc(growths,95)} p99={pc(growths,99)} max={max(growths)}")
print(f"rolling 3-turn cum  p90={pc(roll3,90)} p95={pc(roll3,95)} p99={pc(roll3,99)}")
print(f"rolling 5-turn cum  p90={pc(roll5,90)} p95={pc(roll5,95)} p99={pc(roll5,99)}")
print(f"rolling 8-turn cum  p90={pc(roll8,90)} p95={pc(roll8,95)} p99={pc(roll8,99)}")
print("\nInterpretation: KEEP must exceed the recent verbatim working set you can't afford to lose mid-task.")
print("  = at least the largest single tool-dump (p99 single) so no atomic turn is orphaned,")
print("  + a few working turns of context. rolling5-p90..p95 ~ the practical floor.")
