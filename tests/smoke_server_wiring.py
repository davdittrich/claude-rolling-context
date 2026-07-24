"""Smoke: server.py wires KEEP_TURNS/KEEP_FLOOR env -> compressor + health.

Run: python3 tests/smoke_server_wiring.py
Importing server.py runs module-level RollingCompressor(...) (the wiring under
test) but NOT the listener (guarded by __main__), so this is import-safe.
"""
import importlib
import os
import sys

PROXY = os.path.join(os.path.dirname(__file__), "..", "proxy")
sys.path.insert(0, PROXY)


def load_server():
    for m in ("server", "compressor"):
        if m in sys.modules:
            del sys.modules[m]
    return importlib.import_module("server")


# 1. defaults
for k in ("ROLLING_CONTEXT_KEEP_TURNS", "ROLLING_CONTEXT_KEEP_FLOOR"):
    os.environ.pop(k, None)
srv = load_server()
assert srv.KEEP_TURNS == 8, srv.KEEP_TURNS
assert srv.KEEP_FLOOR == 3, srv.KEEP_FLOOR
assert srv.compressor.keep_turns == 8
assert srv.compressor.keep_floor == 3

# 2. env override + misconfig clamp (floor 10 > turns 8 -> floor clamped to 8)
os.environ["ROLLING_CONTEXT_KEEP_TURNS"] = "8"
os.environ["ROLLING_CONTEXT_KEEP_FLOOR"] = "10"
srv = load_server()
assert srv.KEEP_TURNS == 8
assert srv.KEEP_FLOOR == 10          # raw env value
assert srv.compressor.keep_turns == 8
assert srv.compressor.keep_floor == 8, "clamp floor<=turns"  # clamped inside compressor

# 3. custom values propagate
os.environ["ROLLING_CONTEXT_KEEP_TURNS"] = "6"
os.environ["ROLLING_CONTEXT_KEEP_FLOOR"] = "2"
srv = load_server()
assert srv.compressor.keep_turns == 6
assert srv.compressor.keep_floor == 2

print("smoke_server_wiring: OK (defaults, env override, clamp, propagation)")
