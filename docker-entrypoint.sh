#!/usr/bin/env bash
# data-layer-adapters/docker-entrypoint.sh
#
# Selects the adapter role at run time via ADAPTER_ROLE env var.
# Valid roles:
#   hook    — run redis_publish_hook.py as a long-lived sidecar
#             (default if ADAPTER_ROLE is unset; this is the publish
#             hook referenced by data-layer-adapters/docs/decisions/0001)
#   mcp     — run mcp/server.py on stdio (per MCP protocol 2024-11-05)
#   smoke   — run tests/dual_write_smoke.sh once and exit
#   help    — print this help
#
# Wired in from the Dockerfile via:
#   ENTRYPOINT ["/usr/local/bin/data-layer-entrypoint"]
#   CMD ["hook"]

set -euo pipefail

log() { printf '[adapters %s] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '[adapters FAIL] %s\n' "$*" >&2; exit 3; }

case "${ADAPTER_ROLE:-hook}" in
    hook)
        log "starting redis_publish_hook (long-lived sidecar)"
        exec python3 /app/lib/redis_publish_hook.py
        ;;
    mcp)
        log "starting MCP server on stdio"
        # Ensure /app/lib is on the path so server.py can import write_through.
        export PYTHONPATH="/app:${PYTHONPATH:-}"
        exec python3 /app/mcp/server.py
        ;;
    smoke)
        log "running dual_write_smoke once"
        bash /app/tests/dual_write_smoke.sh
        ;;
    help|--help|-h|"")
        cat <<USAGE
Usage: $0 <hook|mcp|smoke|help>

Environment:
  ADAPTER_ROLE                      hook (default) | mcp | smoke | help
  DATA_LAYER_POSTGRES_DSN           postgres connection string
  DATA_LAYER_REDIS_URL              redis URL (default redis://127.0.0.1:6379/0)
  DATA_LAYER_REDIS_HOST             redis host (used by healthcheck)
  DATA_LAYER_REDIS_PORT             redis port (used by healthcheck)
  DATA_LAYER_FALKORDB_HOST          falkordb RESP host
  DATA_LAYER_FALKORDB_PORT          falkordb RESP port (default 6379)
  DATA_LAYER_HOOK_PROJECTIONS       comma-separated projections to subscribe to
                                    (default session.heartbeat)
USAGE
        exit 0
        ;;
    *)
        fail "unknown role: $1 (see $0 help)"
        ;;
esac
