#!/usr/bin/env python3
"""Replay REAL per-session growth traces through KEEP policies. Opus-1M tiered pricing.
Compare pure-token vs blend(turns+token-band) on COST and a QUALITY proxy.
Quality = fraction of turns whose verbatim tail still covers the last W turns (coherence unit)."""
import os, json, glob

# Opus 4.8: flat 1M-context pricing, NO >200K premium (verified Jul 2026, $5/$25 per MTok).
INP,CW,CR,OUT = 5e-6,6.25e-6,0.50e-6,25e-6
tr_read =lambda C:C*CR
tr_write=lambda C:C*CW
out_rate=lambda C:OUT
TRIGGER=100_000; SUMMARY=3_000; MIN_TURN=6; OUT_TOK=335; W=3   # coherence unit = last 3 turns

def keep_token(win, target):           # keep most-recent turns until >= target tokens
    s=0; k=0
    for g in reversed(win):
        s+=g; k+=1
        if s>=target: break
    return k
def keep_blend(win, N, hi, floor=1):   # keep last N turns; token cap hi; but never below `floor` turns
    k=0; s=0
    for g in reversed(win):
        if k>=N: break
        if k>=floor and s+g>hi: break   # cap binds only once floor satisfied -> floor may exceed hi
        s+=g; k+=1
    return max(min(floor,len(win)), k)

def run(sessions, policy):
    cost=0.0; c5=c8=0; turns=0; comps=0; kt_sum=0; kept1=0; peakwrite=0
    for seq in sessions:
        win=[]; summ=0; last=0
        for i,g in enumerate(seq):
            C=summ+sum(win)+g
            if i==0: cost+=tr_write(C)
            else:    cost+=tr_write(last)+tr_read(max(0,C-last))
            cost+=OUT_TOK*out_rate(C)
            win.append(g); last=g; turns+=1
            C=summ+sum(win)
            if C>TRIGGER and i>=MIN_TURN:
                k=policy(win); comps+=1
                if k<=1: kept1+=1                 # compression that kept only ONE turn (floor bit)
                win=win[-k:]; summ=SUMMARY; last=sum(win)
                peakwrite=max(peakwrite,last)      # worst-case rebuild size
                C=summ+sum(win)
            kt_sum+=sum(win)
            if len(win)>=min(5,i+1): c5+=1
            if len(win)>=min(8,i+1): c8+=1
    n=max(1,turns)
    return dict(cost=cost,c5=c5/n,c8=c8/n,kt=kt_sum/n,comps=comps,
                kept1=kept1,kept1pct=100*kept1/max(1,comps),peak=peakwrite)

# load real traces (sessions that cross trigger — others identical under all policies)
sessions=[]
for f in glob.glob(os.path.expanduser("~/.claude/projects/**/*.jsonl"),recursive=True):
    ctxs=[]
    try:
        with open(f,errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line: continue
                try:o=json.loads(line)
                except:continue
                u=(o.get("message")or{}).get("usage")
                if not isinstance(u,dict):continue
                c=(u.get("input_tokens",0)or 0)+(u.get("cache_creation_input_tokens",0)or 0)+(u.get("cache_read_input_tokens",0)or 0)
                if c>0:ctxs.append(c)
    except:continue
    # clean: a real turn can't grow context beyond the window; >200k jump = session-boundary/resume artifact.
    d=[]
    for i in range(1,len(ctxs)):
        g=ctxs[i]-ctxs[i-1]
        if g>200_000 or g< -30_000:   # boundary/compaction: flush as separate session
            if d and max((sum(d[:j+1]) for j in range(len(d))),default=0)>TRIGGER: sessions.append(d)
            d=[]
        elif g>0:
            d.append(g)
    if d and max((sum(d[:j+1]) for j in range(len(d))),default=0)>TRIGGER:
        sessions.append(d)

print(f"replaying {len(sessions)} sessions that cross {TRIGGER//1000}k")
print(f"{'policy':26} {'cost':>7} {'cov5':>6} {'cov8':>6} {'meanKept':>8} {'comps':>5} {'kept-1turn':>11} {'peakRebuild':>11}")
policies=[
 ("token-40k",lambda w:keep_token(w,40_000)),
 ("blend N=8 hi=50k floor=1",lambda w:keep_blend(w,8,50_000,1)),
 ("blend N=8 hi=50k floor=2",lambda w:keep_blend(w,8,50_000,2)),
 ("blend N=8 hi=50k floor=3",lambda w:keep_blend(w,8,50_000,3)),
 ("blend N=8 hi=40k floor=3",lambda w:keep_blend(w,8,40_000,3)),
 ("blend N=6 hi=45k floor=3",lambda w:keep_blend(w,6,45_000,3)),
]
for name,p in policies:
    r=run(sessions,p)
    print(f"{name:26} ${r['cost']:6.0f} {r['c5']*100:5.1f}% {r['c8']*100:5.1f}% {r['kt']:8.0f} {r['comps']:5} {r['kept1']:4}({r['kept1pct']:4.1f}%) {r['peak']:11.0f}")
