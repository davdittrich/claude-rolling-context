# Opus 4.8: flat 1M-context pricing, NO >200K premium (verified Jul 2026, $5/$25 per MTok).
INP,CW,CR,OUT = 5e-6,6.25e-6,0.50e-6,25e-6
tr_read  = lambda C: C*CR
tr_write = lambda C: C*CW
out_rate = lambda C: OUT
def sim(trigger, keep, turns, g, p_cold, out_tok=335, summary=3000, min_turn=6):
    C=2000; cost=0.0; last=C; uncached=False
    for t in range(turns):
        cold = p_cold>0 and ((t*2654435761)%1000)/1000.0 < p_cold
        cost += tr_write(C) if (uncached or cold) else tr_write(last)+tr_read(max(0,C-last))
        uncached=False
        cost += out_tok*out_rate(C)
        C+=g; last=g
        if trigger and C>trigger and t>=min_turn:
            cost += tr_read(C-keep)+summary*out_rate(C); C=keep+summary; uncached=True
    return cost
SESS=[("median",1610,90),("active",3436,150),("heavy",3436,400)]
print(f"{'session':8} {'out_tok':>7} {'base$':>9} {'comp@100k$':>11} {'saving%':>8}")
for name,g,turns in SESS:
    for ot in (335,1000,2000,4000,8000):
        base=sim(None,40000,turns,g,0.085,out_tok=ot)
        comp=sim(100_000,40000,turns,g,0.085,out_tok=ot)
        print(f"{name:8} {ot:>7} {base:9.2f} {comp:11.2f} {100*(base-comp)/base:7.1f}%")
    print()
