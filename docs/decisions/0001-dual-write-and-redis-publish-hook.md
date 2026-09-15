# 0001 — Dual-write postgres + falkordb via redis publish hook

**Status:** Accepted (per data-layer architecture review, 2026-09-14)
**Context:** Audit-grade ephemeral state is being promoted to
postgres (see `../data-layer-postgres/docs/decisions/0002-...md`).
FalkorDB is used to project that durable state into a traversable
graph for cross-cutting queries (session → tool → message trace,
project hierarchy, framework comparison). The adapter layer is the
right place to keep both writes coherent.

## Decision

For every promoted state category, the adapter performs:

1. Write to **postgres first** (primary source of truth).
2. Write to **redis as cache / hot-state** (advisory TTL only).
3. **Publish** a structured event on a per-adapter redis pub/sub
   channel. A single subscriber (`redis_publish_hook`) consumes the
   event and emits the matching `GRAPH.QUERY` against falkordb.

This is the only sanctioned path. Direct writes to falkordb from
adapters are forbidden (see Consequences).

## Interface

The dual-write lives in `data-layer-adapters/lib/write_through.py`:

```python
class WriteThrough:
    def __init__(self, pg_dsn: str, redis_url: str, falkordb_url: str): ...
    def write(self, record: dict, *, projection: str | None = None) -> None: ...
```

Behavior:

- `record` is the postgres-shaped dict (table, columns).
- Internally: `INSERT/UPSERT` to postgres, then `SETEX` to redis
  under the tenant prefix, then `PUBLISH` the projection hint.
- If the redis publish fails, the postgres write still committed —
  this is intentional. The publish hook retries from the WAL on the
  next event (events are idempotent MERGEs in falkordb).
- If `projection` is None, the falkordb projection is skipped
  (postgres + redis only).

The redis pub/sub subscriber lives in
`data-layer-adapters/lib/redis_publish_hook.py`:

```python
class RedisPublishHook:
    def __init__(self, redis_url: str, falkordb_url: str, graph: str): ...
    def start(self) -> None: ...   # blocks; runs forever
```

It is a long-running process. One per data-layer deployment.

## Phase order

| Phase | Promoted category |
|---|---|
| 1 | Session presence / heartbeat |
| 2 | In-flight tool executions + recent messages |
| 3 | Idempotency keys |
| 4 | Rate-limit events |
| 5 | Lock audit |

Each phase is flag-gated: the publish hook only subscribes to the
channels for categories that are turned on.

## FalkorDB projection rules

- Use `MERGE` (not `CREATE`) for nodes — labels auto-create on first
  use, schema is implicit.
- Multi-statement Cypher is split on `;` and sent statement-by-
  statement (`GRAPH.QUERY` rejects multi-statement payloads).
- All MERGE statements are idempotent; replaying a missed event is
  safe.

## Consequences

- **Single write path.** Adapters never write falkordb directly; the
  publish hook is the only falkordb writer for projected state.
- **Failure containment.** A redis or falkordb outage degrades the
  graph (stale or absent), never the primary (postgres). Recovery
  is replay from postgres via a follow-up job (out of scope here).
- **Throughput cost.** Two writes per record (postgres + redis) plus
  one publish. The publish hook does the falkordb write
  asynchronously, so adapter latency is dominated by postgres.
- **Schema coupling.** Projection logic is duplicated between the
  publish hook and the seed/bootstrap payload. They MUST agree on
  labels, edge types, and MERGE shape — see
  `../data-layer-falkordb/docs/graph-schema.md` as the canonical
  reference.

## See also

- `../data-layer-postgres/docs/decisions/0002-audit-grade-ephemeral-state-promoted-to-postgres.md`
  — the categories this ADR applies to.
- `../data-layer-redis/docs/decisions/0001-key-pattern-catalog.md`
  — the matching redis key inventory used by the write-through.
- `../data-layer-falkordb/docs/graph-schema.md` — graph shape that
  the publish hook must reproduce.
- `../data-layer-falkordb/docs/decisions/0002-data-layer-recommendations-handoff.md`
  — parent handoff naming the phases.
