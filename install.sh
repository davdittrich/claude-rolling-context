#!/usr/bin/env bash
# Install the Rolling Context plugin for Claude Code.
#
# Pure stdlib — no pip install needed. Just requires Python 3.7+.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROXY_DIR="$SCRIPT_DIR/proxy"
PORT="${ROLLING_CONTEXT_PORT:-5588}"
PROXY_URL="http://127.0.0.1:$PORT"

echo "=== Rolling Context Proxy Installer ==="
echo ""

# 1. Check Python is available
echo "[1/3] Checking Python..."
if command -v python3 &>/dev/null; then
    PY_VERSION=$(python3 --version 2>&1)
    echo "  Found $PY_VERSION (pure stdlib — no pip install needed)"
elif command -v python &>/dev/null; then
    PY_VERSION=$(python --version 2>&1)
    echo "  Found $PY_VERSION (pure stdlib — no pip install needed)"
else
    echo "  ERROR: Python not found. Install Python 3.7+ and try again."
    exit 1
fi

# 2. Configure ANTHROPIC_BASE_URL in Claude Code settings.json
echo "[2/3] Configuring Claude Code settings.json..."

SETTINGS_FILE="$HOME/.claude/settings.json"
mkdir -p "$HOME/.claude"

PY_CMD=""
if command -v python3 &>/dev/null; then PY_CMD="python3"
elif command -v python &>/dev/null; then PY_CMD="python"
fi

$PY_CMD - "$SETTINGS_FILE" "$PROXY_URL" "$PROXY_DIR" <<'PYEOF'
import json, os, sys

settings_file, proxy_url, proxy_dir = sys.argv[1], sys.argv[2], sys.argv[3]

# One predicate (spec section 9). The old guard here was `"127.0.0.1" not in existing`,
# which classified any loopback address as ourselves -- exactly how a foreign proxy on
# :8787 read as "already installed".
sys.path.insert(0, proxy_dir)
import chain

settings = {}
if os.path.exists(settings_file):
    try:
        with open(settings_file, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, IOError) as exc:
        # Regenerating a file we could not parse would discard the user's own settings.
        print(f"  SKIPPED: {settings_file} is not valid JSON ({exc})")
        print("  Fix it by hand and re-run this installer.")
        sys.exit(0)

if not isinstance(settings.get("env"), dict):
    settings["env"] = {}

env = settings["env"]

existing = env.get("ANTHROPIC_BASE_URL", "")
if not existing:
    env["ANTHROPIC_BASE_URL"] = proxy_url
    print(f"  Set ANTHROPIC_BASE_URL={proxy_url}")
elif chain.is_self(existing):
    print(f"  ANTHROPIC_BASE_URL already points at rolling-context ({chain.display(existing)})")
else:
    # Write nothing. Chaining silently here is an unrecorded change no undo can see.
    print(f"  ANTHROPIC_BASE_URL is held by {chain.display(existing)} — writing nothing.")
    print("  rolling-context is out of the request path. To put it back, run")
    print("  /rolling-context:chain inside a project (or hooks/chain.sh chain).")

# Set plugin config defaults (only if not already present)
defaults = {
    "ROLLING_CONTEXT_PORT": "5588",
    "ROLLING_CONTEXT_TRIGGER": "100000",
    "ROLLING_CONTEXT_TARGET": "40000",
}
for key, value in defaults.items():
    if key not in env:
        env[key] = value

# Unset ROLLING_CONTEXT_MODEL = compress with the session's own model
# (prompt-cache hit). Migrate away the old seeded haiku default.
if env.get("ROLLING_CONTEXT_MODEL") == "claude-haiku-4-5-20251001":
    del env["ROLLING_CONTEXT_MODEL"]

with open(settings_file, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print(f"  Settings written to {settings_file}")
PYEOF

# Test seam: run the detection and seeding logic, then stop before touching anything else.
if [ -n "${ROLLING_CONTEXT_NO_START:-}" ]; then
    exit 0
fi

# 3. Register plugin
echo "[3/3] Registering Claude Code plugin..."

PLUGIN_LINK="$HOME/.claude/plugins/rolling-context"
mkdir -p "$HOME/.claude/plugins"

if [ -L "$PLUGIN_LINK" ] || [ -d "$PLUGIN_LINK" ]; then
    rm -rf "$PLUGIN_LINK"
fi
ln -s "$SCRIPT_DIR" "$PLUGIN_LINK"
echo "  Plugin linked at $PLUGIN_LINK"

echo ""
echo "=== Installation Complete ==="
echo ""
echo "The proxy will auto-start when you launch Claude Code."
echo "To start it manually: cd $PROXY_DIR && python3 server.py"
echo ""
echo "Configuration (via environment variables):"
echo "  ROLLING_CONTEXT_PORT    = $PORT"
echo "  ROLLING_CONTEXT_TRIGGER = ${ROLLING_CONTEXT_TRIGGER:-80000} tokens"
echo "  ROLLING_CONTEXT_TARGET  = ${ROLLING_CONTEXT_TARGET:-40000} tokens"
echo ""
echo "Start a new Claude Code session to activate the proxy."
