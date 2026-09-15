# 0002 — Phase 3 cutover procedure (flag-gated)

**Status:** Accepted (per data-layer architecture review, 2026-09-14)
**Context:** Per ADR `0001-dual-write-and-redis-publish-hook.md`,
each promoted state category is enabled independently by a flag. This
ADR captures the operational procedure for flipping those flags on a
live deployment, category by category.

## Per-category flags

The flag map lives in
`data-layer-adapters/lib/write_through.py:_CATEGORY_FLAGS`:

| Category | Flag env var |
|---|---|
| Session presence / heartbeat | `DATA_LAYER_DW_SESSION_PRESENCE` |
| Tool execution lifecycle | `DATA_LAYER_DW_TOOL_EXECUTION` |
| Idempotency keys | `DATA_LAYER_DW_IDEMPOTENCY_KEY` |
| Recent messages | `DATA_LAYER_DW_RECENT_MESSAGES` |
| Rate-limit events | `DATA_LAYER_DW_RATE_LIMIT_EVENT` |
| Lock audit | `DATA_LAYER_DW_LOCK_AUDIT` |

All flags default to `false` (postgres-only). Flipping a flag on
enables the full dual-write path (postgres + redis + publish) for
that category in every adapter process that picks up the new env.

## Cutover order

| Phase | Category | Notes |
|---|---|---|
| 1 | session_presence | Smallest blast radius; one row per heartbeat. Easy to roll back. |
| 2 | tool_execution | Heavier; one row per tool call. Verify the `tool_executions_latest_attempt` view before flipping. |
| 3 | idempotency_key | Adds churn to `idempotency_keys`; verify the `claim_idempotency_key` function exists before flipping. |
| 4 | rate_limit_event | Deferred (per `0002` postgres ADR). Apply migration 0007 first. |
| 5 | lock_audit | Deferred (per `0002` postgres ADR). Apply migration 0008 first. |

## Per-category procedure

For each category in the order above:

1. **Verify prerequisites** — confirm the postgres migration exists
   and is applied. Example for session_presence:

   ```sql
   SELECT 1 FROM information_schema.tables
   WHERE table_name = 'session_heartbeats';
   ```

2. **Verify the redis key family** — confirm the destination in the
   redis key-pattern catalog (`data-layer-redis/docs/decisions/0001-...`).

3. **Cut one tenant at a time.** Use a tenant-prefix override:

   ```
   DATA_LAYER_REDIS_PREFIX=dl:tenant-a:
   DATA_LAYER_DW_SESSION_PRESENCE=true
   ```

   Restart the publish hook (one process per data-layer deployment)
   with the same env. Confirm:

   - redis key appears under `dl:tenant-a:session:<id>:presence`
   - postgres `session_heartbeats` row exists
   - falkordb `Session.last_heartbeat_at` updates within ~1s

4. **Cut all tenants** by flipping the flag in the global env. Confirm
   the publish hook counters (`get_counters()` from `write_through.py`)
   show non-zero `pg_writes_per_s` and `publish_emit_per_s`.

5. **Hold for one observation window** before flipping the next
   category. Window length: at least one full TTL cycle (default
   300s) so the cache and the projection have settled.

## Rollback

To roll back a category: set the flag to `false` and restart the
adapter processes. The postgres primary is unchanged (we always
write postgres first). The redis cache will age out by TTL. The
falkordb projection will go stale — that is acceptable per the ADR
(postgres remains source of truth).

A full replay-from-postgres recovery (out of scope here) would
re-emit MERGE statements for every record in the affected tables.

## What this ADR does NOT cover

- Cross-tenant consistency. Each tenant is cut independently.
- Backfill. We accept starting fresh; no historical replay.
- The replay-from-postgres recovery path (out of scope; mentioned in
  ADR 0001 as a future job).

## See also

- `0001-dual-write-and-redis-publish-hook.md` — the dual-write contract.
- `../data-layer-postgres/docs/decisions/0002-audit-grade-ephemeral-state-promoted-to-postgres.md`
  — the categories being cut over.
- `../data-layer-redis/docs/decisions/0001-key-pattern-catalog.md`
  — the redis destination per category.
- `lib/write_through.py` — the flag map implementation.
- `lib/redis_publish_hook.py` — the publish hook process.
