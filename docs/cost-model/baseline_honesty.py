# Opus 4.8: flat 1M-context pricing, NO >200K premium (verified Jul 2026, $5/$25 per MTok).
INP,CW,CR,OUT = 5e-6,6.25e-6,0.50e-6,25e-6
tr_read  = lambda C: C*CR
tr_write = lambda C: C*CW
out_rate = lambda C: OUT
WINDOW=1_000_000
def sim(trigger, keep, turns, g, p_cold=0.085, out_tok=335, summary=3000, min_turn=6):
    C=2000; cost=0.0; last=C; uncached=False; peak=C; capped=False
    for t in range(turns):
        cold = p_cold>0 and ((t*2654435761)%1000)/1000.0 < p_cold
        cost += tr_write(C) if (uncached or cold) else tr_write(last)+tr_read(max(0,C-last))
        uncached=False
        cost += out_tok*out_rate(C)
        C+=g; last=g; peak=max(peak,C)
        if C>WINDOW: capped=True
        if trigger and C>trigger and t>=min_turn:
            cost += tr_read(C-keep)+summary*out_rate(C); C=keep+summary; uncached=True
    return cost,peak,capped

for name,g,turns in [("active",3436,150),("heavy",3436,400)]:
    print(f"\n===== {name}: g={g}/turn, {turns} turns =====")
    for ot in (335,3000):
        print(f"  --- out_tok={ot} (thinking {'light' if ot==335 else 'heavy'}) ---")
        rows=[
          ("unbounded (strawman)", None, 40000),
          ("native compact @160k", 160_000, 40000),
          ("native compact @800k/keep120k", 800_000, 120_000),
          ("PROXY @100k/keep40k", 100_000, 40000),
        ]
        vals={}
        for lbl,tr,k in rows:
            c,pk,cap=sim(tr,k,turns,g,out_tok=ot)
            vals[lbl]=c
            flag=" [EXCEEDS 1M WINDOW - unphysical]" if cap else ""
            print(f"     {lbl:32} ${c:8.2f}  peak={pk//1000:>4}k{flag}")
        p=vals["PROXY @100k/keep40k"]
        for lbl in ("unbounded (strawman)","native compact @160k","native compact @800k/keep120k"):
            b=vals[lbl]; print(f"        proxy vs {lbl:32}: {100*(b-p)/b:+5.1f}%")
