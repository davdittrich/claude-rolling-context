#!/usr/bin/env python3
"""Rolling-compression optimum. REAL telemetry + Opus-4.8 1M flat pricing. No globals mutated."""
# Opus 4.8: flat 1M-context pricing, NO >200K premium (verified Jul 2026, $5/$25 per MTok).
# The >200K long-context tier was a Sonnet-4/4.5-era feature, retired with the 4.6 generation.
INP,CW,CR,OUT = 5e-6,6.25e-6,0.50e-6,25e-6  # input; 5m cache-write 1.25x; cache-read 0.1x; output 5x
tr_read  = lambda C: C*CR
tr_write = lambda C: C*CW
out_rate = lambda C: OUT

def sim(trigger, keep, turns, g, p_cold, out_tok=335, summary=3000, min_turn=6):
    C=2000; cost=0.0; last=C; uncached=False
    for t in range(turns):
        cold = p_cold>0 and ((t*2654435761) % 1000)/1000.0 < p_cold  # deterministic, non-phase-locked
        cost += tr_write(C) if (uncached or cold) else tr_write(last)+tr_read(max(0,C-last))
        uncached=False
        cost += out_tok*out_rate(C)
        C+=g; last=g
        if trigger and C>trigger and t>=min_turn:
            cost += tr_read(C-keep) + summary*out_rate(C)
            C=keep+summary; uncached=True
    return cost

SESS=[("median  G=1610 T=90",1610,90),("active  G=3436 T=150",3436,150),("heavy   G=3436 T=400",3436,400)]
print("=== TRIGGER sweep (keep=40k, p_cold=0.085) ===")
for name,g,turns in SESS:
    base=sim(None,40000,turns,g,0.085)
    grid=[(tr,sim(tr,40000,turns,g,0.085)) for tr in range(50_000,200_001,2_500)]
    btr,bc=min(grid,key=lambda x:x[1])
    print(f"\n{name}  no-compress=${base:.2f}  OPT_trigger={btr//1000}k (${bc:.2f}, {100*(bc-base)/base:+.0f}% vs none)")
    for tr in (60_000,80_000,100_000,120_000,140_000):
        c=sim(tr,40000,turns,g,0.085); print(f"   {tr//1000:>3}k ${c:7.2f}  vs-opt {100*(c-bc)/bc:+4.1f}%")

print("\n=== KEEP sweep (trigger=80k, active) ===")
for k in (15_000,20_000,30_000,40_000,60_000):
    print(f"   keep={k//1000:>2}k ${sim(80_000,k,150,3436,0.085):.2f}")
print("\n=== joint OPT (active) ===")
best=min(((tr,k,sim(tr,k,150,3436,0.085)) for tr in range(50_000,180_001,5_000) for k in range(15_000,60_001,5_000)),key=lambda x:x[2])
print(f"   trigger={best[0]//1000}k keep={best[1]//1000}k -> ${best[2]:.2f}")
