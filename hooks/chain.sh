#!/usr/bin/env bash
# The single implementation entry point (D6). Slash commands and the dead-upstream
# error body both route through this, so its path is load-bearing user-facing text.
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")/../proxy" && pwd)/chain.py" "$@"
