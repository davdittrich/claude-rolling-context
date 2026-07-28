#!/usr/bin/env bash
# Ensure rolling context proxy is running
# Pure stdlib — no venv needed, just python

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROXY_DIR="$SCRIPT_DIR/../proxy"
PIDFILE="$HOME/.claude/rolling-context-proxy.pid"
VERFILE="$HOME/.claude/rolling-context-proxy.version"
HOOKLOG="$HOME/.claude/rolling-context-hook.log"
PORT="${ROLLING_CONTEXT_PORT:-5588}"
PROXY_URL="http://127.0.0.1:$PORT"
CURRENT_VERSION=$(cat "$SCRIPT_DIR/../.claude-plugin/plugin.json" 2>/dev/null | grep '"version"' | head -1 | sed 's/.*"version".*"\(.*\)".*/\1/')

log() {
    # R1: every diagnostic goes to stderr AND the log. stdout belongs to the one
    # JSON object SessionStart is allowed to emit (Fact 2), and nothing else.
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$HOOKLOG" >&2
}

# Detect Windows (git bash)
if [[ "$(uname -s)" == MINGW* ]] || [[ "$(uname -s)" == MSYS* ]]; then
    IS_WINDOWS=true
else
    IS_WINDOWS=false
fi

log "Hook started. PROXY_DIR=$PROXY_DIR IS_WINDOWS=$IS_WINDOWS"

# Who actually holds ANTHROPIC_BASE_URL, across every scope Claude Code reads.
#
# Root bug #2: this used to read only ~/.claude/settings.json, while `headroom wrap claude`
# writes the project's .claude/settings.local.json -- which outranks it. The displacing value
# was never in the file we looked at, so we printed "already" and went quiet.
#
# Root bug #1: the old guard was `elif "127.0.0.1" not in existing`, which called ANY loopback
# address us. headroom on :8787 read as "already installed". Classification is now the one
# shared predicate, chain.py is-self, which compares against the port we actually bind.
SETTINGS_FILE="$HOME/.claude/settings.json"
CHAIN="$PROXY_DIR/chain.py"

if [ "$IS_WINDOWS" = true ]; then
    PY_CMD="python"
elif command -v python3 &>/dev/null; then
    PY_CMD="python3"
else
    PY_CMD="python"
fi

# Seeds the plugin config defaults, and claims ANTHROPIC_BASE_URL only when passed "write".
seed_settings() {
    "$PY_CMD" - "$SETTINGS_FILE" "$PROXY_URL" "${1:-}" <<'PYSEED'
import json, os, sys

settings_file, proxy_url, mode = sys.argv[1], sys.argv[2], sys.argv[3]

settings = {}
if os.path.exists(settings_file):
    try:
        with open(settings_file, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, IOError) as exc:
        # Never regenerate a file we could not parse -- that silently discards the user's
        # own settings. Leave it exactly as found and say so.
        sys.stderr.write(f"refusing to rewrite {settings_file}: {exc}\n")
        sys.exit(1)

if not isinstance(settings.get("env"), dict):
    settings["env"] = {}
env = settings["env"]

if mode == "write":
    env["ANTHROPIC_BASE_URL"] = proxy_url

# Plugin config defaults (only if not already present)
for key, value in {
    "ROLLING_CONTEXT_PORT": "5588",
    "ROLLING_CONTEXT_TRIGGER": "100000",
    "ROLLING_CONTEXT_TARGET": "40000",
}.items():
    env.setdefault(key, value)

# Unset ROLLING_CONTEXT_MODEL = compress with the session's own model
# (prompt-cache hit). Migrate away the old seeded haiku default.
if env.get("ROLLING_CONTEXT_MODEL") == "claude-haiku-4-5-20251001":
    del env["ROLLING_CONTEXT_MODEL"]

with open(settings_file, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
PYSEED
}

seed() {
    if seed_settings "$1" 2>>"$HOOKLOG"; then
        return 0
    fi
    log "WARNING: could not update $SETTINGS_FILE"
    return 1
}

EFFECTIVE=$("$PY_CMD" "$CHAIN" effective-abu 2>>"$HOOKLOG")
ABU_RC=$?
ALERT=""

if [ "$ABU_RC" -ne 0 ]; then
    log "WARNING: could not resolve ANTHROPIC_BASE_URL — leaving settings untouched"
elif [ -z "$EFFECTIVE" ]; then
    seed write && log "Set ANTHROPIC_BASE_URL=$PROXY_URL ($SETTINGS_FILE)"
elif "$PY_CMD" "$CHAIN" is-self "$EFFECTIVE" 2>>"$HOOKLOG"; then
    seed
    log "ANTHROPIC_BASE_URL already points at us ($EFFECTIVE)"
else
    # Foreign. Write nothing -- an automatic, unrecorded chain is invisible to any undo.
    # The user gets one alert and one command; nothing here needs a restart.
    seed
    log "Displaced: $EFFECTIVE holds ANTHROPIC_BASE_URL — writing nothing"
    ALERT=$("$PY_CMD" "$CHAIN" should-alert "$EFFECTIVE" 2>>"$HOOKLOG")
fi

if [ -z "$ALERT" ]; then
    ALERT=$("$PY_CMD" "$CHAIN" drifted 2>>"$HOOKLOG")
fi
if [ -n "$ALERT" ]; then
    printf '%s\n' "$ALERT"
fi

# Test seam: run the detection and seeding logic, then stop before starting anything.
if [ -n "${ROLLING_CONTEXT_NO_START:-}" ]; then
    exit 0
fi

# Check if proxy is already running
_kill_pid() {
    local pid="$1"
    if [ "$IS_WINDOWS" = true ]; then
        powershell.exe -Command "Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue" 2>/dev/null
    else
        kill "$pid" 2>/dev/null
        sleep 1
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
    fi
}

_pid_alive() {
    local pid="$1"
    if [ "$IS_WINDOWS" = true ]; then
        powershell.exe -Command "if (Get-Process -Id $pid -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" 2>/dev/null
    else
        kill -0 "$pid" 2>/dev/null
    fi
}

if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if _pid_alive "$PID"; then
        # Check if version changed — restart if so
        RUNNING_VERSION=$(cat "$VERFILE" 2>/dev/null)
        if [ "$CURRENT_VERSION" = "$RUNNING_VERSION" ]; then
            log "Proxy already running (PID $PID, v$RUNNING_VERSION)"
            exit 0
        fi
        log "Version changed ($RUNNING_VERSION -> $CURRENT_VERSION), restarting proxy (PID $PID)"
        _kill_pid "$PID"
    fi
    rm -f "$PIDFILE" "$VERFILE"
fi

# Start proxy directly — no venv needed (pure stdlib)
log "Starting proxy..."
(
    cd "$PROXY_DIR" || { log "ERROR: cannot cd to $PROXY_DIR"; exit 1; }
    PYTHON_CMD=""
    if [ "$IS_WINDOWS" = true ]; then
        PYTHON_CMD="python"
    elif command -v python3 &>/dev/null; then
        PYTHON_CMD="python3"
    else
        PYTHON_CMD="python"
    fi
    nohup $PYTHON_CMD server.py > "$HOME/.claude/rolling-context-proxy.log" 2>&1 &
    echo $! > "$PIDFILE"
    echo "$CURRENT_VERSION" > "$VERFILE"
    log "Proxy started with PID $! (v$CURRENT_VERSION)"
) &

exit 0
