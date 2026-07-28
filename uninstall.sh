#!/usr/bin/env bash
# Uninstall the Rolling Context plugin.

set -e

_SRC="$0"
_DIR="${_SRC%/*}"
[ "$_DIR" = "$_SRC" ] && _DIR="."
SCRIPT_DIR="$(cd "$_DIR" && pwd)"
PROXY_DIR="$SCRIPT_DIR/proxy"
CHAIN="$SCRIPT_DIR/hooks/chain.sh"

CLAUDE_DIR="$HOME/.claude"
PIDFILE="$CLAUDE_DIR/rolling-context-proxy.pid"
PLUGIN_LINK="$CLAUDE_DIR/plugins/rolling-context"
MARKETPLACE_CACHE="$CLAUDE_DIR/plugins/cache/rolling-context-marketplace"
MARKETPLACE_DIR="$CLAUDE_DIR/plugins/marketplaces/rolling-context-marketplace"
PORT="${ROLLING_CONTEXT_PORT:-5588}"

echo "=== Uninstalling Rolling Context ==="

# Stop proxy — try PID file first, then find by port
STOPPED=false
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "Stopped proxy (PID $PID)"
        STOPPED=true
    fi
    rm -f "$PIDFILE"
fi
rm -f "$CLAUDE_DIR/rolling-context-proxy.version"
if [ "$STOPPED" = false ]; then
    PROXY_PID=$(lsof -ti:$PORT 2>/dev/null || ss -tlnp "sport = :$PORT" 2>/dev/null | grep -oP 'pid=\K\d+' || true)
    if [ -n "$PROXY_PID" ]; then
        kill $PROXY_PID 2>/dev/null || true
        echo "Stopped proxy on port $PORT"
    fi
fi

# Remove all log files
rm -f "$CLAUDE_DIR/rolling-context-proxy.log"
rm -f "$CLAUDE_DIR/rolling-context-proxy.log.err"
rm -f "$CLAUDE_DIR/rolling-context-debug.log"
rm -f "$CLAUDE_DIR/rolling-context-hook.log"

# Undo any project-scope ANTHROPIC_BASE_URL chain.py wrote, before the plugin directory
# that holds chain.sh (below) is removed -- the tool that undoes the chain must still
# exist on disk when it runs.
if [ -x "$CHAIN" ]; then
    "$CHAIN" unchain --all || echo "  WARNING: unchain --all failed; a project's ANTHROPIC_BASE_URL may still point at this proxy"
fi
rm -f "$CLAUDE_DIR/rolling-context-proxy.json"

# Clean ANTHROPIC_BASE_URL from Claude Code settings.json
SETTINGS_FILE="$CLAUDE_DIR/settings.json"
if [ -f "$SETTINGS_FILE" ]; then
    PY_CMD=""
    if command -v python3 &>/dev/null; then PY_CMD="python3"
    elif command -v python &>/dev/null; then PY_CMD="python"
    fi
    if [ -z "$PY_CMD" ]; then
        echo "  SKIPPED: no python3/python found -- could not clean ANTHROPIC_BASE_URL from $SETTINGS_FILE"
    fi
    if [ -n "$PY_CMD" ]; then
        $PY_CMD - "$SETTINGS_FILE" "$PROXY_DIR" <<'PYEOF'
import json, sys, os

settings_file, proxy_dir = sys.argv[1], sys.argv[2]
sys.path.insert(0, proxy_dir)
import chain

try:
    with open(settings_file, "r") as f:
        settings = json.load(f)
except (json.JSONDecodeError, IOError):
    sys.exit(0)

env = settings.get("env", {})
current = env.get("ANTHROPIC_BASE_URL", "")
upstream = env.get("ROLLING_CONTEXT_UPSTREAM", "")

if current and chain.is_self(current):
    if upstream:
        env["ANTHROPIC_BASE_URL"] = upstream
        del env["ROLLING_CONTEXT_UPSTREAM"]
        print(f"Restored ANTHROPIC_BASE_URL to {upstream}")
    else:
        del env["ANTHROPIC_BASE_URL"]
        print("Removed ANTHROPIC_BASE_URL")
elif "ROLLING_CONTEXT_UPSTREAM" in env:
    del env["ROLLING_CONTEXT_UPSTREAM"]

# Remove plugin config vars
for key in list(env.keys()):
    if key.startswith("ROLLING_CONTEXT_"):
        del env[key]

settings["env"] = env
with open(settings_file, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
PYEOF
    fi
fi

# Remove plugin link (manual install)
if [ -L "$PLUGIN_LINK" ] || [ -d "$PLUGIN_LINK" ]; then
    rm -rf "$PLUGIN_LINK"
    echo "Removed plugin link"
fi

# Remove marketplace-installed plugin cache
if [ -d "$MARKETPLACE_CACHE" ]; then
    rm -rf "$MARKETPLACE_CACHE"
    echo "Removed marketplace plugin cache"
fi

# Remove marketplace registration
if [ -d "$MARKETPLACE_DIR" ]; then
    rm -rf "$MARKETPLACE_DIR"
    echo "Removed marketplace registration"
fi

# Clean installed_plugins.json
INSTALLED_FILE="$CLAUDE_DIR/plugins/installed_plugins.json"
if [ -f "$INSTALLED_FILE" ] && command -v python3 &>/dev/null; then
    python3 -c "
import json
with open('$INSTALLED_FILE') as f:
    data = json.load(f)
if 'rolling-context@rolling-context-marketplace' in data.get('plugins', {}):
    del data['plugins']['rolling-context@rolling-context-marketplace']
    with open('$INSTALLED_FILE', 'w') as f:
        json.dump(data, f, indent=2)
    print('Removed from installed plugins')
"
fi

# Clean known_marketplaces.json
MARKETPLACES_FILE="$CLAUDE_DIR/plugins/known_marketplaces.json"
if [ -f "$MARKETPLACES_FILE" ] && command -v python3 &>/dev/null; then
    python3 -c "
import json
with open('$MARKETPLACES_FILE') as f:
    data = json.load(f)
if 'rolling-context-marketplace' in data:
    del data['rolling-context-marketplace']
    with open('$MARKETPLACES_FILE', 'w') as f:
        json.dump(data, f, indent=2)
    print('Removed marketplace')
"
fi


echo ""
echo "Uninstalled."
