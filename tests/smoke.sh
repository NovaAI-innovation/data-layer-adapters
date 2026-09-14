#!/usr/bin/env bash
# data-layer-adapters/tests/smoke.sh — verify adapter collection is reachable.
# Future: validate per-adapter bootstrap, mcp server connectivity, seed idempotency.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
exec bash "$ROOT/bootstrap" status
