#!/usr/bin/env python3
"""Parse Claude Code transcripts -> per-session arrival streams + lengths.
Dumps sessions.json = [{n_turns, growths:[...positive per-turn deltas...], peak, first}].
growths = content arriving each turn (the proxy's 'arrival process'); native-compaction
resets (<-30k) and resume artifacts (>200k) are dropped, stream concatenated (the proxy
would prevent native resets, so we model one continuous arrival run per conversation)."""
import os, json, glob

files = glob.glob(os.path.expanduser("~/.claude/projects/**/*.jsonl"), recursive=True)
sessions = []
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
    if len(ctxs) < 3:
        continue
    growths = []
    for i in range(1, len(ctxs)):
        g = ctxs[i] - ctxs[i - 1]
        if g > 200_000 or g < -30_000:   # boundary / native-compaction / resume artifact
            continue
        if g > 0:
            growths.append(g)
    if not growths:
        continue
    sessions.append(dict(n_turns=len(ctxs), n_growth=len(growths),
                         growths=growths, peak=max(ctxs), first=ctxs[0]))

out = os.path.join(os.path.dirname(__file__), "sessions.json")
with open(out, "w") as fh:
    json.dump(sessions, fh)
tot_turns = sum(s["n_turns"] for s in sessions)
print(f"parsed {len(sessions)} sessions, {tot_turns} turns -> {out}")
print(f"turn counts: min={min(s['n_turns'] for s in sessions)} "
      f"max={max(s['n_turns'] for s in sessions)}")
