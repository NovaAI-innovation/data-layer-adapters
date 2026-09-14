#!/usr/bin/env bash
# agent-zero/lib/deps.sh — install Agent Zero python deps.
#
# Installs psycopg + mcp into Agent Zero's framework venv.
set -euo pipefail
VENV="${DATA_LAYER_ADAPTERS_A0_A0_VENV:-/opt/venv-a0}"
log()  { printf '[deps %s] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '[deps FAIL] %s\n' "$*" >&2; exit 3; }
[[ -x "$VENV/bin/pip" ]] || fail "venv missing or pip not executable: $VENV"
log "installing psycopg + mcp into $VENV"
"$VENV/bin/pip" install --quiet --upgrade psycopg mcp
log "deps installed"
