#!/usr/bin/env python3
"""Profiling spike for e86.8 (per-request rehash) + e86.10 (TLS handshake).
Measures against realistic conversation sizes from the user's telemetry
(median ctx 90k, p75 253k, p90 485k tokens; ~4 chars/token)."""
import os, sys, time, ssl, socket

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "proxy"))
import server  # noqa: E402

def big_text(n_chars):
    return "x" * n_chars

def build_convo(target_tokens):
    """Realistic mix: small user/assistant turns + occasional large tool dumps.
    ~4 chars/token. Returns a messages list near target_tokens."""
    target_chars = target_tokens * 4
    msgs, chars = [], 0
    i = 0
    while chars < target_chars:
        # user turn
        u = {"role": "user", "content": f"user msg {i} " + big_text(300)}
        # assistant, occasionally with a big tool_use + a big tool_result user msg
        if i % 5 == 0:
            a = {"role": "assistant", "content": [
                {"type": "text", "text": "reasoning " + big_text(400)},
                {"type": "tool_use", "input": {"cmd": big_text(200)}},
            ]}
            tr = {"role": "user", "content": [
                {"type": "tool_result", "content": big_text(8000)}]}  # big dump
            batch = [u, a, tr]
        else:
            a = {"role": "assistant", "content": "asst " + big_text(500)}
            batch = [u, a]
        for m in batch:
            msgs.append(m); chars += server._count_chars([m]) if hasattr(server, "_count_chars") else len(str(m))
        i += 1
    return msgs

def time_hash(msgs, reps=20):
    # warm
    server._hash_messages(msgs)
    t0 = time.perf_counter()
    for _ in range(reps):
        server._hash_messages(msgs)
    return (time.perf_counter() - t0) / reps * 1000  # ms

print("=== e86.8: _hash_messages per-request cost (whole conversation, every request) ===")
print(f"{'ctx tokens':>12} {'#msgs':>7} {'ms/request':>11} {'per-100-turn session (s)':>24}")
for toks in (30_000, 90_000, 253_000, 485_000, 879_000):
    msgs = build_convo(toks)
    ms = time_hash(msgs)
    # cumulative: called once per request; a session reaching this size did ~len(msgs)/2 turns,
    # each turn re-hashing the then-current prefix. Approx cumulative = ms * n_turns (upper bound at full size).
    n_turns = len(msgs) // 2
    session_s = ms * n_turns / 1000  # worst-case (all turns at ~full size); real is ~half
    print(f"{toks:>12,} {len(msgs):>7} {ms:>11.2f} {session_s:>24.1f}")

print("\n=== e86.10: TLS handshake cost vs LLM round-trip ===")
host = "api.anthropic.com"
try:
    ctx = ssl.create_default_context()
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        s = socket.create_connection((host, 443), timeout=10)
        ss = ctx.wrap_socket(s, server_hostname=host)
        times.append((time.perf_counter() - t0) * 1000)
        ss.close()
    times.sort()
    print(f"  TLS connect+handshake to {host}: median {times[len(times)//2]:.1f} ms (n=5: {[round(t) for t in times]})")
    print(f"  vs a typical LLM streamed response: ~2000-15000 ms")
except Exception as e:
    print(f"  (could not measure handshake: {e})")
