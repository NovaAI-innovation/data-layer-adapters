#!/usr/bin/env bash
# agent-zero/lib/plugin.sh — install Agent Zero plugin code.
#
# Copies data-layer-adapters/agent-zero/plugin/* into A0's plugin
# directory so Agent Zero discovers and loads it on next boot.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
SRC="$ROOT/plugin"
DEST="${DATA_LAYER_ADAPTERS_A0_A0_PLUGINS_DIR:-/a0/usr/plugins}/data_management"
log()  { printf '[plugin %s] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '[plugin FAIL] %s\n' "$*" >&2; exit 3; }
[[ -d "$SRC" ]] || fail "plugin source missing: $SRC"
mkdir -p "$(dirname "$DEST")"
rsync -a --delete "$SRC/" "$DEST/"
log "plugin installed at $DEST"
