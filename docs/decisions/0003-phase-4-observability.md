# 0003 — Phase 4 observability (lightweight counters)

**Status:** Accepted (per data-layer architecture review, 2026-09-14)
**Context:** With dual-writes flowing for multiple promoted
categories, operators need a real-time view of throughput and lag
without standing up a full Prometheus + Grafana stack on day 1.

## Decision

Ship lightweight in-process counters in
`data-layer-adapters/lib/write_through.py` (`COUNTERS` dataclass,
exposed via `get_counters()` and `reset_counters()`). No Prometheus
exporter initially.

The counters track:

| Counter | Source |
|---|---|
| `pg_writes_per_s` | Successful postgres writes (dual path or postgres-only). |
| `redis_writes_per_s` | Successful redis writes. |
| `falkordb_projection_per_s` | Successful publish-hook projections. |
| `falkordb_projection_failure_per_s` | Failed projections. |
| `publish_emit_per_s` | Successful redis PUBLISH calls. |
| `publish_failure_per_s` | Failed PUBLISH calls. |
| `ttl_housekeeping_lag_rows` | Rows eligible for vacuum but not yet deleted. |
| `uptime_s` | Seconds since the last reset. |

## Why no Prometheus exporter (yet)

- The total number of dual-write processes is small (one per
  adapter deployment + one publish hook). The counters can be
  scraped over an HTTP `/metrics` endpoint or logged periodically
  via cron.
- A Prometheus exporter adds a runtime dependency (`prometheus_client`)
  and an HTTP server surface that has to be authenticated.
- Revisit this decision when:
  - More than 3 adapter processes run per deployment, OR
  - Cross-deployment aggregation becomes a hard requirement.

## Plumbing

- `write_through.py` updates the counters at the end of every
  successful write step. The counters are in-memory and per-process;
  restart resets them to zero (this is intentional; persistent
  counters belong in a metrics server, not the application).
- The publish hook should call `get_counters()` on a periodic
  interval (every 30s) and emit a structured log line. The exact
  cadence is operator-configurable via `LOG_COUNTERS_INTERVAL_S`
  (default 30; 0 disables).

## Logging shape

A single log line per interval, one counter block per line:

```json
{
  "ts": "2026-09-14T21:38:46Z",
  "component": "write_through",
  "counters": {
    "pg_writes_per_s": 12.4,
    "redis_writes_per_s": 12.1,
    "falkordb_projection_per_s": 12.0,
    "falkordb_projection_failure_per_s": 0.0,
    "publish_emit_per_s": 12.1,
    "publish_failure_per_s": 0.0,
    "ttl_housekeeping_lag_rows": 0,
    "uptime_s": 6421.0
  }
}
```

The same shape is used by the publish hook (`component:
"redis_publish_hook"`).

## TTL housekeeping

`ttl_housekeeping_lag_rows` is the number of rows in the promoted
postgres tables (e.g. `session_heartbeats`, `idempotency_keys`,
`tool_executions`) that have `expires_at < now()` and have not yet
been vacuumed. The vacuum job is out of scope for this ADR; the
counter exists so operators can spot when vacuum lag is growing.

## What this ADR does NOT cover

- A Prometheus exporter. Revisit later.
- Cross-process aggregation. Each process is its own island.
- Alert rules. Operators are expected to read the log lines.

## See also

- `0001-dual-write-and-redis-publish-hook.md` — the dual-write contract.
- `0002-phase-3-cutover-procedure.md` — how categories come online.
- `lib/write_through.py:COUNTERS` — implementation.
- `../data-layer-postgres/docs/decisions/0002-audit-grade-ephemeral-state-promoted-to-postgres.md`
  — the categories being counted.