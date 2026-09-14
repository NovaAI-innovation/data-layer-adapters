#!/usr/bin/env bash
# hermes-agent/lib/deps.sh — install Hermes python deps (placeholder).
#
# Concrete deps depend on the Hermes runtime. Override this file when
# the Hermes integration is implemented.
set -euo pipefail
VENV="${DATA_LAYER_ADAPTERS_HERMES_HERMES_VENV:-/opt/venv-hermes}"
log()  { printf '[hermes-deps %s] %s\n' "$(date +%H:%M:%S)" "$*"; }
if [[ -x "$VENV/bin/pip" ]]; then
  log "installing minimal hermes deps into $VENV (override when concrete deps are known)"
  "$VENV/bin/pip" install --quiet --upgrade requests || true
  log "deps step complete (placeholder)"
else
  log "no hermes venv at $VENV; install path is implementation-specific"
fi
