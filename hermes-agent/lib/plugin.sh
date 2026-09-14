#!/usr/bin/env bash
# hermes-agent/lib/plugin.sh — install Hermes plugin code (placeholder).
#
# Hermes uses Nous Research's own plugin discovery. Concrete install
# path is implementation-specific to the Hermes runtime.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
SRC="$ROOT/plugin"
log()  { printf '[hermes-plugin %s] %s\n' "$(date +%H:%M:%S)" "$*"; }
if [[ -d "$SRC" && "$(ls -A "$SRC" 2>/dev/null)" ]]; then
  log "plugin source has files; install path is implementation-specific to Hermes runtime"
  log "source: $SRC"
else
  log "no plugin files yet (hermes plugin code lives at $SRC)"
fi
