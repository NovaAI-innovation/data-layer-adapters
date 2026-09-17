#!/usr/bin/env bash
# agent-zero/lib/deps.sh — install Agent Zero python deps.
#
# Installs psycopg + mcp into Agent Zero's framework venv.
# Idempotent: if the venv already has importable psycopg + mcp (e.g. because
# the a0-init step populated it in the A0 image where the pip is libc-compatible),
# this step is a no-op. Running pip from a DIFFERENT base image against the
# shared volume breaks because the pip binary's ELF interpreter is libc-bound
# to the image it was installed in.
set -euo pipefail
VENV="${DATA_LAYER_ADAPTERS_A0_A0_VENV:-/opt/venv-a0}"
log()  { printf '[deps %s] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '[deps FAIL] %s\n' "$*" >&2; exit 3; }

# Already-populated check: does the shared venv already have what we need?
if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c 'import psycopg, mcp' 2>/dev/null; then
  log "deps already present in $VENV (psycopg + mcp importable); skipping install"
  exit 0
fi

# Fallback: pip install (only works if pip binary is compatible with THIS image)
[[ -x "$VENV/bin/pip" ]] || fail "venv missing or pip not executable: $VENV"
log "installing psycopg + mcp into $VENV"
"$VENV/bin/pip" install --quiet --upgrade 'psycopg[binary]' mcp
log "deps installed"
