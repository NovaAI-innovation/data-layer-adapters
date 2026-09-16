# TOOLS_AND_WIRING.md — data-layer-adapters

> **Scope.** `data-layer-adapters` owns the universal MCP (`mcp/`), the framework-adapter collection (`agent-zero/`, `hermes-agent/`), the dual-write lib (`lib/`), and the dispatcher (`bootstrap`). It is the single agent-facing surface that talks to both Postgres and Qdrant.
>
> **Authority rule (binding).** This document mirrors the implementation in `mcp/server.py`, `mcp/tools/*.py`, `lib/*.py`, `agent-zero/bootstrap`, `hermes-agent/bootstrap`, and the top-level `bootstrap`. `mcp/server.py` is authoritative for tool behaviour; if a future change diverges, update this file in the same PR.
>
> **Audit rule.** Per `docs/SUBMODULE_OWNERSHIP.md` rule #1+4: no cross-submodule imports; MCP exposes reads; writes go through the bootstrap scripts. The audit trigger list in `SUBMODULE_OWNERSHIP.md` (`Adding a write path`, `Promoting a rag.ingest.* tool to runtime`, `Cross-submodule data flow change`) re-opens this doc.

---

## 1. Tool inventory (21 MCP tools)

The MCP server (`mcp/server.py`, MCP protocol `2024-11-05`, server `name: data-layer`, `version: 0.3.0`) registers 21 tools, dispatched by `tools/call` through `TOOL_REGISTRY` (postgres) ∪ `TOOL_REGISTRY_RAG` (Qdrant) ∪ `FRAMEWORK_DESCRIPTORS` (currently empty after the heartbeat removal). All write paths are gated by `mcp/tools/safety.py`.

| Backend | Tool name | Read/Write | Gating | Backend target | Primary input schema | Return shape |
|---|---|---|---|---|---|---|
| postgres | `health.check` | read | none | DSN, 10 tables | `{}` | `{ok, tables:{t: count}, error?}` |
| postgres | `projects.list` | read | none | `projects` | `{status?: enum, limit?: 0..1000}` | `{rows, count}` |
| postgres | `projects.get` | read | none | `projects` | `{project_key: string}` | row or `{error}` |
| postgres | `agents.list` | read | none | `agents`+`projects`+`agent_frameworks` | `{project_key?, framework?, status?, limit?}` | `{rows, count}` |
| postgres | `agents.get` | read | none | `agents`+`agent_frameworks` | `{id: uuid}` | row or `{error}` |
| postgres | `sessions.list` | read | none | `sessions` | `{agent_id?, status?, since?, until?, limit?}` | `{rows, count}` |
| postgres | `sessions.get` | read | none | `sessions` | `{id: uuid}` | row or `{error}` |
| postgres | `messages.list` | read | none | `messages` | `{session_id?, agent_id?, role?, since?, until?, limit?}` | `{rows, count}` (content truncated to 4000 chars) |
| postgres | `messages.get` | read | none | `messages` | `{id: uuid}` | full row |
| postgres | `messages.search` | read | none | `messages` (ILIKE) | `{q, agent_id?, session_id?, role?, since?, until?, limit?}` | `{rows, count, q}` (snippet, ranked by recency) |
| postgres | `tool_executions.list` | read | none | `tool_executions` | `{session_id?, agent_id?, tool_name?, status?, since?, until?, limit?}` | `{rows, count}` |
| postgres | `tool_executions.get` | read | none | `tool_executions` | `{id: uuid}` | full row |
| postgres | `history.retrieve` | read | none | `messages` + `tool_executions` (UNION) | `{q, agent_id?, role?, since?, until?, limit?:<=200}` | `{snippets:[{source,...}], count, q}` |
| postgres | `execute_sql` | read | **SELECT-only gate** (`safety.assert_select_only`) | DSN | `{sql, statement_timeout_ms?: 1000..120000}` | `{columns, rows, count, truncated, limit}` |
| qdrant | `rag.health` | read | none | `GET /healthz` | `{}` | `{ok, qdrant_url, status_code, body}` |
| qdrant | `rag.collections.list` | read | none | `GET /collections` | `{}` | `{collections, qdrant_url}` |
| qdrant | `rag.collection.info` | read | collection-allowlist | `GET /collections/{name}` | `{collection}` | `{collection, info}` |
| qdrant | `rag.search` | read | collection-allowlist + always-on SOT `must_not` filter | `POST /collections/{name}/points/search` | `{collection, vector:number[], limit?:<=100, filter?, with_payload?, with_vector?}` | `{collection, qdrant_url, result}` |
| qdrant | `rag.ingest.status` | read | none | marker file (default `/opt/qdrant/state/last_ingest.json`) | `{marker_path?}` | `{marker_path, last_ingest}` |
| qdrant | `rag.ingest.point` | **write** | **`MCP_INSTALL_MODE=1`** + SOT-invariant | `PUT /collections/{name}/points` | `{collection, id, vector, payload}` | `{collection, point_id, qdrant_url, result}` |
| qdrant | `rag.ingest.batch` | **write** | **`MCP_INSTALL_MODE=1`** + SOT-invariant (per row, skip on failure) | `PUT /collections/{name}/points` (CSV) | `{collection, csv_path, id_column?, vector_column?, payload_columns?, batch_size?:1..256}` | `{collection, csv_path, uploaded, batches, skipped_sot, skipped_missing_id, skipped_count, skipped_sample}` |

**Collection allowlist** (only these collections are touchable via `rag.*`): `mpg_source_authority_documents`, `mpg_emails`. See `data-layer-qdrant/SCHEMAS.md`.

**Refused write verbs** (returned by `safety.assert_select_only` before any cursor is opened): `INSERT`, `UPDATE`, `DELETE`, `DROP`, `CREATE`, `ALTER`, `TRUNCATE`, `GRANT`, `REVOKE`, `MERGE`, `CALL`, `COPY`, `LOCK`, `VACUUM`, `REINDEX`, `CLUSTER`, `REFRESH`, `CHECKPOINT`. Allowed leading tokens: `SELECT`, `WITH`, `VALUES`, `EXPLAIN`, `SHOW`.

---

## 2. Per-tool reference (21 sections)

### 2.1 `health.check`

- **Description.** Probe DSN reachability and per-table existence + row counts.
- **Input schema.** `{}` (no inputs).
- **Output.** `{"ok": bool, "tables": {<table>: <count>, ...}, "error"?: string}`.
- **Read or write.** Read.
- **Gating.** None.
- **Underlying SQL.** `SELECT 1 AS ok`, then `SELECT count(*) FROM <t>` for each of the 10 hard-coded tables in `EXPECTED_TABLES` (`projects`, `agent_frameworks`, `agents`, `agent_skills`, `agent_plugins`, `available_tools`, `hooks`, `sessions`, `messages`, `tool_executions`).
- **Postgres tables touched.** All 10 above; cross-link `data-layer-postgres/SCHEMAS.md` §1–§10.
- **Example call.**
  ```json
  {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"health.check","arguments":{}}}
  ```

### 2.2 `projects.list`

- **Description.** List projects, optionally filtered by status.
- **Input schema.** `{status?: "active"|"archived", limit?: 0..1000, default 50}`.
- **Output.** `{rows: [...], count: int}`.
- **Read or write.** Read.
- **Gating.** None.
- **Underlying SQL.** `SELECT id, project_key, display_name, description, status, created_at, updated_at FROM projects [WHERE status = %s] ORDER BY created_at DESC LIMIT %s`.
- **Postgres table touched.** `projects` (see `data-layer-postgres/SCHEMAS.md` §1).
- **Example call.**
  ```json
  {"name":"projects.list","arguments":{"status":"active","limit":10}}
  ```

### 2.3 `projects.get`

- **Description.** Get one project by business key (`project_key`).
- **Input schema.** `{project_key: string}` (required).
- **Output.** Row or `{error: "no project with project_key=..."}` with code `-32004`.
- **Read or write.** Read.
- **Gating.** None.
- **Underlying SQL.** `SELECT id, project_key, display_name, description, status, metadata, created_at, updated_at FROM projects WHERE project_key = %s`.
- **Postgres table touched.** `projects`.
- **Example call.** `{ "name":"projects.get","arguments":{"project_key":"default"} }`.

### 2.4 `agents.list`

- **Description.** List agents with optional filters (`project_key`, `framework`, `status`).
- **Input schema.** `{project_key?, framework?, status?: "active"|"disabled"|"archived", limit?: 0..1000}`.
- **Output.** `{rows, count}`.
- **Read or write.** Read.
- **Gating.** None.
- **Underlying SQL.** `SELECT a.id, a.project_id, a.framework_id, f.kind AS framework_kind, f.display_name AS framework_display_name, a.framework_local_id, a.deployment, a.display_name, a.profile_key, a.status, a.created_at, a.updated_at FROM agents a [JOIN projects p ON p.id = a.project_id] [JOIN agent_frameworks f ON f.id = a.framework_id] [WHERE p.project_key = %s AND f.kind = %s AND a.status = %s] ORDER BY a.created_at DESC LIMIT %s`.
- **Postgres tables touched.** `agents`, `projects`, `agent_frameworks` (cross-link `data-layer-postgres/SCHEMAS.md` §1, §2, §3).
- **Example call.** `{ "name":"agents.list","arguments":{"framework":"agent_zero","status":"active"} }`.

### 2.5 `agents.get`

- **Description.** Get one agent by `id` (uuid).
- **Input schema.** `{id: uuid}` (required).
- **Output.** Row or `{error}`.
- **Read or write.** Read.
- **Gating.** None.
- **Underlying SQL.** `SELECT a.id, a.project_id, a.framework_id, f.kind AS framework_kind, f.display_name AS framework_display_name, a.framework_local_id, a.deployment, a.display_name, a.profile_key, a.status, a.metadata, a.created_at, a.updated_at FROM agents a JOIN agent_frameworks f ON f.id = a.framework_id WHERE a.id = %s`.
- **Postgres tables touched.** `agents`, `agent_frameworks`.
- **Example call.** `{ "name":"agents.get","arguments":{"id":"<uuid>"} }`.

### 2.6 `sessions.list`

- **Description.** List sessions with optional filters (`agent_id`, `status`, `since`, `until`).
- **Input schema.** `{agent_id?: uuid, status?: "active"|"closed"|"crashed", since?: date-time, until?: date-time, limit?: 0..1000}`.
- **Output.** `{rows, count}`.
- **Read or write.** Read.
- **Gating.** None.
- **Underlying SQL.** `SELECT id, agent_id, session_key, status, started_at, ended_at, metadata FROM sessions [WHERE agent_id = %s AND status = %s AND started_at >= %s AND started_at < %s] ORDER BY started_at DESC LIMIT %s`.
- **Postgres table touched.** `sessions` (cross-link `data-layer-postgres/SCHEMAS.md` §8).
- **Example call.** `{ "name":"sessions.list","arguments":{"agent_id":"<uuid>","status":"active","limit":25} }`.

### 2.7 `sessions.get`

- **Description.** Get one session by `id` (uuid).
- **Input schema.** `{id: uuid}` (required).
- **Output.** Row or `{error}`.
- **Read or write.** Read.
- **Gating.** None.
- **Underlying SQL.** `SELECT id, agent_id, session_key, status, started_at, ended_at, metadata FROM sessions WHERE id = %s`.
- **Postgres table touched.** `sessions`.
- **Example call.** `{ "name":"sessions.get","arguments":{"id":"<uuid>"} }`.

### 2.8 `messages.list`

- **Description.** List messages with optional filters; `content` truncated to 4000 chars.
- **Input schema.** `{session_id?, agent_id?, role?: "user"|"assistant"|"tool"|"system", since?, until?, limit?: 0..1000}`.
- **Output.** `{rows, count}`.
- **Read or write.** Read.
- **Gating.** None.
- **Underlying SQL.** `SELECT id, session_id, agent_id, direction, role, content_type, substring(content from 1 for 4000) AS content, thread_id, parent_message_id, created_at FROM messages [WHERE ...] ORDER BY created_at DESC LIMIT %s`.
- **Postgres table touched.** `messages` (cross-link `data-layer-postgres/SCHEMAS.md` §9).
- **Example call.** `{ "name":"messages.list","arguments":{"session_id":"<uuid>","role":"assistant"} }`.

### 2.9 `messages.get`

- **Description.** Get one message by `id` (uuid). Full content returned (no truncation).
- **Input schema.** `{id: uuid}` (required).
- **Output.** Row or `{error}`.
- **Read or write.** Read.
- **Gating.** None.
- **Underlying SQL.** `SELECT id, session_id, agent_id, direction, peer_agent_id, role, content, content_type, thread_id, parent_message_id, external_ref, created_at FROM messages WHERE id = %s`.
- **Postgres table touched.** `messages`.
- **Example call.** `{ "name":"messages.get","arguments":{"id":"<uuid>"} }`.

### 2.10 `messages.search`

- **Description.** ILIKE search over `messages.content` with optional filters; returns ranked snippet rows.
- **Input schema.** `{q: string, agent_id?, session_id?, role?, since?, until?, limit?: 0..1000}` (`q` required).
- **Output.** `{rows: [{id, session_id, agent_id, role, snippet, created_at}], count, q}`.
- **Read or write.** Read.
- **Gating.** None (input escaped via `_escape_like` + `ESCAPE 0x7c`).
- **Underlying SQL.** `SELECT id, session_id, agent_id, role, substring(content from 1 for 4000) AS snippet, created_at FROM messages WHERE content ILIKE %s ESCAPE 0x7c [AND ...] ORDER BY created_at DESC LIMIT %s`.
- **Postgres table touched.** `messages`.
- **Example call.** `{ "name":"messages.search","arguments":{"q":"postgres","limit":20} }`.

### 2.11 `tool_executions.list`

- **Description.** List tool executions with optional filters.
- **Input schema.** `{session_id?, agent_id?, tool_name?, status?: "pending"|"success"|"error"|"blocked", since?, until?, limit?: 0..1000}`.
- **Output.** `{rows, count}`.
- **Read or write.** Read.
- **Gating.** None.
- **Underlying SQL.** `SELECT id, agent_id, session_id, message_id, tool_name, arguments, result, status, started_at, finished_at, duration_ms, error FROM tool_executions [WHERE ...] ORDER BY started_at DESC LIMIT %s`.
- **Postgres table touched.** `tool_executions` (cross-link `data-layer-postgres/SCHEMAS.md` §10).
- **Example call.** `{ "name":"tool_executions.list","arguments":{"tool_name":"execute_sql","status":"error"} }`.

### 2.12 `tool_executions.get`

- **Description.** Get one tool execution by `id` (uuid). Full payload returned.
- **Input schema.** `{id: uuid}` (required).
- **Output.** Row or `{error}`.
- **Read or write.** Read.
- **Gating.** None.
- **Underlying SQL.** `SELECT id, agent_id, session_id, message_id, tool_name, arguments, result, status, started_at, finished_at, duration_ms, error, parent_execution_id, idempotency_key, attempt_number, retry_of_execution_id, external_ref FROM tool_executions WHERE id = %s`.
- **Postgres table touched.** `tool_executions`.
- **Example call.** `{ "name":"tool_executions.get","arguments":{"id":"<uuid>"} }`.

### 2.13 `history.retrieve`

- **Description.** Composite retrieval across `messages` + `tool_executions`, ranked by recency.
- **Input schema.** `{q: string, agent_id?, role?, since?, until?, limit?:<=200, default 20}` (`q` required).
- **Output.** `{snippets: [{source: "message"|"tool_execution", ...}], count, q}`.
- **Read or write.** Read.
- **Gating.** None.
- **Underlying SQL.** Two parallel queries: messages by `content ILIKE %s ESCAPE 0x7c` (snippet 400 chars) and tool_executions by `tool_name ILIKE %s OR error ILIKE %s OR arguments::text ILIKE %s` (snippet 400 chars). Results merged and sorted by `created_at` DESC.
- **Postgres tables touched.** `messages`, `tool_executions`.
- **Example call.** `{ "name":"history.retrieve","arguments":{"q":"heartbeat","limit":50} }`.

### 2.14 `execute_sql`

- **Description.** Strictly SELECT-only SQL. Refuses 17 write verbs before opening the cursor.
- **Input schema.** `{sql: string, statement_timeout_ms?: 1000..120000, default 30000}` (`sql` required).
- **Output.** `{columns: string[], rows: object[], count: int, truncated: bool, limit: int}`.
- **Read or write.** **Read** (write verbs refused at `safety.assert_select_only`).
- **Gating.** **SELECT-only gate** — `_WRITE_VERBS = (INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE, GRANT, REVOKE, MERGE, CALL, COPY, LOCK, VACUUM, REFRESH, CHECKPOINT)`; `_ALLOWED_LEAD_TOKENS = {SELECT, WITH, VALUES, EXPLAIN, SHOW}`. Per-transaction `SET LOCAL statement_timeout = N` and row-cap default `1000` (`DEFAULT_SELECT_ROW_LIMIT`).
- **Underlying SQL.** Caller-provided `SELECT` (or `WITH`/`VALUES`/`EXPLAIN`/`SHOW`).
- **Postgres tables touched.** Any; cross-link `data-layer-postgres/SCHEMAS.md`.
- **Example call.** `{ "name":"execute_sql","arguments":{"sql":"SELECT count(*) FROM sessions","statement_timeout_ms":15000} }`.

### 2.15 `rag.health`

- **Description.** Qdrant liveness probe (`GET /healthz`).
- **Input schema.** `{}`.
- **Output.** `{ok: bool, qdrant_url: string, status_code: int, body: string (truncated 500), error?: string}`.
- **Read or write.** Read.
- **Gating.** None.
- **Underlying HTTP call.** `GET QDRANT_URL + /healthz` (plain-text response; bypasses the JSON `_qdrant_get` helper).
- **Qdrant collection touched.** None (service-level only).
- **Example call.** `{ "name":"rag.health","arguments":{} }`.

### 2.16 `rag.collections.list`

- **Description.** List all Qdrant collections visible to the MCP.
- **Input schema.** `{}`.
- **Output.** `{collections: [...], qdrant_url: string}`.
- **Read or write.** Read.
- **Gating.** None (allowlist gate applies to writes only).
- **Underlying HTTP call.** `GET /collections`.
- **Qdrant collection touched.** None.
- **Example call.** `{ "name":"rag.collections.list","arguments":{} }`.

### 2.17 `rag.collection.info`

- **Description.** Schema + point count for a single collection. Collection must be in the allowlist.
- **Input schema.** `{collection: string}` (required).
- **Output.** `{collection: string, info: {...}}` or `{error}`.
- **Read or write.** Read.
- **Gating.** **Collection allowlist** (`mpg_source_authority_documents`, `mpg_emails`).
- **Underlying HTTP call.** `GET /collections/{name}`.
- **Qdrant collection touched.** Caller-supplied (from allowlist); see `data-layer-qdrant/SCHEMAS.md`.
- **Example call.** `{ "name":"rag.collection.info","arguments":{"collection":"mpg_source_authority_documents"} }`.

### 2.18 `rag.search`

- **Description.** Vector similarity search. Always-on SOT `must_not` filter excludes `do_not_ingest_y_n='Y'` and `lifecycle_status='superseded'`.
- **Input schema.** `{collection, vector: number[], limit?:<=100 default 10, filter?: object, with_payload?: bool default true, with_vector?: bool default false}` (`collection`, `vector` required).
- **Output.** `{collection, qdrant_url, result}`.
- **Read or write.** Read.
- **Gating.** **Collection allowlist** + **always-on SOT `must_not`** (caller cannot disable).
- **Underlying HTTP call.** `POST /collections/{name}/points/search` with body `{vector, limit, with_payload, with_vector, filter:{must:[...], must_not:[...]+sot}}`.
- **Qdrant collection touched.** Caller-supplied (from allowlist); 768-dim cosine (`sentence-transformers/all-mpnet-base-v2`).
- **Example call.**
  ```json
  {"name":"rag.search","arguments":{"collection":"mpg_emails","vector":[0.0, 0.0, "..."],"limit":10}}
  ```

### 2.19 `rag.ingest.status`

- **Description.** Read the JSON marker file the bootstrap writes after a successful seed run.
- **Input schema.** `{marker_path?: string}` (defaults to env `DATA_LAYER_QDRANT_INGEST_MARKER` or `/opt/qdrant/state/last_ingest.json`).
- **Output.** `{marker_path: string, last_ingest: object|null, note?: "no ingest marker found..."}`.
- **Read or write.** Read.
- **Gating.** None.
- **Underlying HTTP call.** None (reads marker file directly).
- **Qdrant collection touched.** None.
- **Example call.** `{ "name":"rag.ingest.status","arguments":{}}`.

### 2.20 `rag.ingest.point`

- **Description.** Single-point upsert. **Gated write.**
- **Input schema.** `{collection, id, vector: number[], payload: object}` (all required).
- **Output.** `{collection, point_id, qdrant_url, result}`.
- **Read or write.** **Write.**
- **Gating.** **`MCP_INSTALL_MODE=1`** (env) + **SOT invariant** (`safety.assert_sot_invariant`: refuses `do_not_ingest_y_n='Y'`; requires `lifecycle_status='superseded'` when `superseded_y_n='Y'`).
- **Underlying HTTP call.** `PUT /collections/{name}/points` body `{"points":[{"id":..., "vector":..., "payload":...}]}`.
- **Qdrant collection touched.** Caller-supplied (from allowlist).
- **Example call.**
  ```json
  {"name":"rag.ingest.point","arguments":{"collection":"mpg_source_authority_documents","id":"<sha256>","vector":[0.0,"..."],"payload":{"relative_path":"...","sha256":"...","lifecycle_status":"active"}}}
  ```

### 2.21 `rag.ingest.batch`

- **Description.** CSV-driven batch upsert. **Gated write.** Offending rows are skipped (with reasons), not aborted.
- **Input schema.** `{collection, csv_path, id_column?: default "point_id", vector_column?: default "embedding_zero", payload_columns?: string[], batch_size?: 1..256 default 64}` (`collection`, `csv_path` required).
- **Output.** `{collection, csv_path, qdrant_url, uploaded, batches, skipped_sot, skipped_missing_id, skipped_count, skipped_sample}`.
- **Read or write.** **Write.**
- **Gating.** **`MCP_INSTALL_MODE=1`** + **SOT invariant** per row.
- **Underlying HTTP call.** `PUT /collections/{name}/points` (one call per `batch_size` window).
- **Qdrant collection touched.** Caller-supplied (from allowlist).
- **Example call.**
  ```json
  {"name":"rag.ingest.batch","arguments":{"collection":"mpg_source_authority_documents","csv_path":"/opt/qdrant/state/source_authority_fixture.csv","batch_size":64}}
  ```

---

## 3. Five-dimension sub-table (sample for `rag.search`)

The MCP tool contract is best summarized by five orthogonal axes. Below is the matrix for one representative tool; the same axes apply to every tool in §2.

| Axis | `rag.search` |
|---|---|
| **1. Retrieval impact** | Returns top-N vectors from a Qdrant collection; payload + score; caller can request `with_vector` to receive the raw 768 floats. |
| **2. Mutation** | None (read-only). |
| **3. Gating** | Collection allowlist; always-on `must_not` SOT filter (caller cannot disable). |
| **4. Backend target** | `data-layer-qdrant` HTTP service on `DATA_LAYER_QDRANT_URL` (default `http://localhost:6333`). |
| **5. Schema authority** | `data-layer-qdrant/SCHEMAS.md` — collection payload shape; `mpg_source_authority_documents` and `mpg_emails` only. |

For the 14 postgres-backed tools the backend target column collapses to `data-layer-postgres` (DSN) and the schema authority column collapses to `data-layer-postgres/SCHEMAS.md`.

---

## 4. Lib scripts

`lib/` hosts the dual-write interface and the long-lived publish hook. There are exactly two source files (no `__pycache__`).

### 4.1 `lib/write_through.py` (347 lines)

| Field | Value |
|---|---|
| **Path** | `/a0/usr/projects/data-layer/data-layer-adapters/lib/write_through.py` |
| **Purpose** | Dual-write client (Postgres → Redis → publish). Per ADR `docs/decisions/0001-dual-write-and-redis-publish-hook.md`. |
| **When called** | Invoked **directly** by adapter code (not through the MCP — `mcp/server.py` exposes reads only). The Phase 3 cutover flips per-category flags. |
| **What it writes/reads** | Reads: `pg_dsn`, `redis_url`, env flags per category. Writes: Postgres tables in `data-layer-postgres/SCHEMAS.md` §1–§13 (per category); Redis keys under `dl:` prefix with `DATA_LAYER_REDIS_TTL` (default 300s) TTL; redis pub/sub channels `dl:pubsub:<projection>`. |
| **Dual-write role** | Primary: writes to postgres. Then: writes advisory TTL to redis cache. Then: publishes the event so the publish hook can project to falkordb. |
| **Sub-objects** | `WriteThrough` class; `_Counters` (Phase 4 observability); `category_enabled(category)`; `get_counters()`, `reset_counters()`. |
| **Categories** | `session_presence` (`DATA_LAYER_DW_SESSION_PRESENCE`), `tool_execution`, `idempotency_key`, `recent_messages`, `rate_limit_event`, `lock_audit`. Default OFF; flag-gated cutover. |
| **Failure modes** | Postgres failure → raises (caller decides). Redis failure → logged; postgres authoritative. Publish failure → logged; idempotent MERGEs in falkordb make replay safe. |

### 4.2 `lib/redis_publish_hook.py` (308 lines)

| Field | Value |
|---|---|
| **Path** | `/a0/usr/projects/data-layer/data-layer-adapters/lib/redis_publish_hook.py` |
| **Purpose** | Long-lived falkordb-projection subscriber. The **only** sanctioned falkordb writer for projected state. |
| **When called** | Runs as a sidecar process: `ADAPTER_ROLE=hook` (default) in `Dockerfile`/`docker-entrypoint.sh`. Subscribes to redis pub/sub channels emitted by `write_through.py` and emits `GRAPH.QUERY` against falkordb. |
| **What it writes/reads** | Reads: redis pub/sub events. Writes: falkordb `GRAPH.QUERY <graph> <query>` (default graph `data_layer`). |
| **Dual-write role** | The redis-side projection half: translates `write_through` publishes into Cypher MERGEs. |
| **Sub-objects** | `FalkorRESP` (minimal RESP client), `RedisPublishHook` (subscriber loop). |
| **Failure modes** | Restart-safe: pub/sub events are not durable; a missed event must be replayed by an out-of-band job (documented in ADR). Multi-statement Cypher rejected by `GRAPH.QUERY`; `redis_publish_hook` splits on `;` and issues one query per statement. |
| **Env vars** | `DATA_LAYER_REDIS_URL`, `DATA_LAYER_FALKORDB_HOST`, `DATA_LAYER_FALKORDB_PORT` (default `127.0.0.1:6379`), `DATA_LAYER_HOOK_PROJECTIONS` (default `session.heartbeat`). |

**There is no third file under `lib/`** (only `__pycache__` excluded). Adding a new module is a structural change requiring an ADR.

---

## 5. Dual-write path (wiring + failure analysis)

The wiring mirrors the canonical diagram in `docs/SUBMODULE_OWNERSHIP.md` ("Wiring route map" section). The adapter collection is the only place that knows the full chain.

```
   Adapter code (e.g., agent-zero plugin hook)
        │
        ▼
   lib/write_through.py::WriteThrough.write()
        │
        ├──► (1) psycopg INSERT/UPSERT into postgres  ──►  data-layer-postgres  (durable; primary)
        │                                                          │
        │                                                          └─► NOTIFY (postgres side; consumer = lib/redis_publish_hook.py)
        │
        ├──► (2) redis SETEX under dl:<tenant>:<key>  ──► data-layer-redis  (advisory TTL, default 300s)
        │
        └──► (3) redis PUBLISH on dl:pubsub:<projection> ──► data-layer-redis  (pub/sub channel)
                                                                       │
                                                                       ▼
                                                       lib/redis_publish_hook.py  (sidecar)
                                                                       │
                                                                       └──► GRAPH.QUERY <graph> <MERGE …> ──► data-layer-falkordb
```

**Step list (one full successful cycle):**

1. Caller invokes `WriteThrough.from_env().write({category, table, row, cache, projection, cypher, params})`.
2. `WriteThrough.write()` checks `category_enabled(category)` — refuses silently if the per-category flag is off (Phase 3 cutover).
3. Opens (lazy) a `psycopg` connection; executes the table write inside a transaction; **commit** before any redis work.
4. Calls `redis.set(key, value, ex=ttl)` with `DATA_LAYER_REDIS_PREFIX` (default `dl:`) + the caller's key.
5. Calls `redis.publish(channel_prefix + projection, payload)` (default channel prefix `dl:pubsub:`, default projections `(session.heartbeat,)`).
6. The publish hook (`lib/redis_publish_hook.py`, separate process) consumes the event, splits Cypher on `;`, and issues `GRAPH.QUERY` calls against falkordb using `FalkorRESP`.
7. Counters are bumped (`COUNTERS.pg_writes`, `redis_writes`, `publish_emits`, `falkordb_projection_attempts`).

**Failure-mode matrix**

| Scenario | Postgres | Redis | Publish | Graph | Recovery |
|---|---|---|---|---|---|
| All success | ✅ committed | ✅ SETEX | ✅ PUBLISH | ✅ MERGE | normal |
| **PG ✅ + Redis ❌** | ✅ committed | error logged | not attempted | stale/absent | cache is stale; postgres is SOT; the next publish for the same record re-projects |
| **PG ✅ + Redis ✅ + Publish ❌** | ✅ committed | ✅ SETEX | error logged | stale/absent | idempotent MERGE on the next event for the same record |
| **PG ❌** | raises | not attempted | not attempted | unchanged | caller aborts; nothing else is allowed to commit |
| Hook down (event missed) | ✅ committed | ✅ SETEX | ✅ PUBLISH (lost) | absent | restart-safe; replay from postgres via out-of-band job (out of scope here) |
| FalkorDB down | ✅ committed | ✅ SETEX | ✅ PUBLISH (buffered in redis) | error logged | idempotent MERGEs make replay safe; counter `falkordb_projection_failures` increments |

The rule: **postgres is the only authoritative write**. Every other layer is recoverable from it. The dual-write path is best-effort past step (1).

---

## 6. Framework adapters

The collection ships exactly two framework adapters (`AGENTS.md` §"MVP framework set"). Both follow the same shape: `bootstrap` + `lib/{deps,plugin}.sh` + `seeds/`. Adding a third requires an ADR.

### 6.1 `agent-zero/`

| Field | Value |
|---|---|
| **Bootstrap** | `agent-zero/bootstrap` (38 lines). Subcommands: `install`, `verify`, `status`, `reset`, `seed`. |
| **Bootstrap behaviour** | `install` → `lib/deps.sh` + `lib/plugin.sh`. `seed` → applies `seeds/0001_default_agent.sql` (always) and `seeds/0002_migrate_local_id.sql` (only when `DATA_LAYER_RUN_MIGRATE` is set). `reset` → no-op. |
| **`lib/deps.sh`** | Installs `psycopg` + `mcp` into `$DATA_LAYER_ADAPTERS_A0_A0_VENV` (default `/opt/venv-a0`). |
| **`lib/plugin.sh`** | rsyncs `agent-zero/plugin/` → `${DATA_LAYER_ADAPTERS_A0_A0_PLUGINS_DIR}/data_management/` (default `/a0/usr/plugins/data_management/`). Preserves a deployed `config.json` (operator state) across reinstalls. |
| **`seeds/0001_default_agent.sql`** | Idempotent. Substitutes `__LOCAL_CONTAINER_NAME__` via `sed`. Inserts `agent_frameworks(kind='agent_zero')`, `projects(project_key='default')`, and one `agents` row keyed by `(framework_id, framework_local_id, deployment)`. **Postgres tables touched:** `agent_frameworks`, `projects`, `agents` (cross-link `data-layer-postgres/SCHEMAS.md` §1, §2, §3). |
| **`seeds/0002_migrate_local_id.sql`** | One-shot operator migration. Updates any pre-existing row with `framework_local_id = 'agent-zero-sbzm'` and empty `deployment` to use `__LOCAL_CONTAINER_NAME__` + `'local'`. Idempotent. Run once. |
| **`plugin/`** | The Agent Zero plugin named `data_management` (`plugin.yaml`). Files: `plugin.yaml`, `default_config.yaml`, `hooks.py`, `execute.py`, `adapter_contract.py`, `helpers/db.py`, `tools/execute_sql.py`, `adapters/chat_json.py`, `adapters/__init__.py`, `prompts/agent.system.tool.execute_sql.md`, `README.md`. |
| **`plugin/execute.py`** | CLI (`stats`/`schema`/`ingest`/`ingest-chat`) for ingest of `/a0/usr/chats/<id>/chat.json` into the persistence schema via `ChatJsonAdapter`. |
| **`plugin/tools/execute_sql.py`** | Agent-callable `execute_sql` tool. Default `readonly=True`; `force=true` required for `DROP`/`TRUNCATE`/`ALTER`. Resolves DSN from `data_management.dsn` plugin config or `EVENT_LEDGER_DSN` env. **Distinct from the MCP `execute_sql` tool**: this is the in-plugin, governance-loosened variant that the framework exposes; the MCP's `execute_sql` (§2.14) is strictly SELECT-only and refuses all 17 write verbs. |
| **`plugin/hooks.py`** | `install()` warns when `psycopg` is missing in `/opt/venv-a0`. `pre_update()` / `uninstall()` are no-ops. |
| **`plugin/adapter_contract.py`** | Framework-agnostic `PersistenceAdapter` abstract class + dataclasses (`Framework`, `Project`, `Agent`, `Session`, `Message`, `ToolExecution`, `AvailableTool`, `AgentSkill`, `AgentPlugin`, `Hook`). The implementation lives at `data-layer-postgres` boundary; this file is the interface. |

### 6.2 `hermes-agent/`

| Field | Value |
|---|---|
| **Bootstrap** | `hermes-agent/bootstrap` (35 lines). Subcommands: `install`, `verify`, `status`, `reset`, `seed`. |
| **Bootstrap behaviour** | `install` → `lib/deps.sh` + `lib/plugin.sh`. `seed` → applies `seeds/0001_seed_hermes.sql` after substituting `__HERMES_CONTAINER_NAME__`. `reset` → no-op. |
| **`lib/deps.sh`** | Placeholder. Conditionally installs `requests` into `$DATA_LAYER_ADAPTERS_HERMES_HERMES_VENV` (default `/opt/venv-hermes`). "Override when concrete deps are known" per the file comment. |
| **`lib/plugin.sh`** | Placeholder. Hermes uses Nous Research's own plugin discovery; "Concrete install path is implementation-specific to the Hermes runtime". Logs the source path; no copy. |
| **`seeds/0001_seed_hermes.sql`** | Idempotent. Inserts `agent_frameworks(kind='hermes', display_name='Hermes (Nous Research)', version=NULL)`, and one `agents` row keyed by `(framework_id, framework_local_id, deployment)` with `metadata = {"kanban": true}`. **Postgres tables touched:** `agent_frameworks`, `projects` (implicit via project_key='default'), `agents` (cross-link `data-layer-postgres/SCHEMAS.md` §2, §3). |
| **Plugin code** | None shipped (placeholder; `hermes-agent/plugin/` does not exist). The seed metadata blob intentionally omits runtime-addressing fields (host/address/mcp_service) per the comment — those belonged to the retired `az-retrieval-mcp` sidecar. The data-layer-adapters universal MCP is now the only sanctioned endpoint. |

### 6.3 Cross-adapter shared shape

Both adapters' `bootstrap` scripts:

- Read `DATA_LAYER_POSTGRES_DSN` (default `postgresql://postgres@localhost:5432/postgres`).
- Substitute a per-adapter container-name placeholder (`__LOCAL_CONTAINER_NAME__` / `__HERMES_CONTAINER_NAME__`) into seed SQL via `sed` before `psql -v ON_ERROR_STOP=1 -X -tA -f`.
- Use the `psql` CLI (not `psycopg`) so the seed runs are observable in any environment with `psql` installed, independent of the MCP runtime.
- Exit code `3` on any failure; `set -euo pipefail`.

The umbrella `bootstrap` discovers framework adapters automatically: `discover_frameworks()` enumerates every direct subdir with an executable `bootstrap` script, excluding `mcp/`, `lib/`, `plugin/`, `prompts/`, `tests/`, `docs/`, and dotfiles. New adapters are picked up with zero glue-code change.

---

## 7. MCP registration (global + per-project history)

### 7.1 Global registration (canonical — **set this turn**)

The MCP is registered **once** at the framework-global level: `/a0/usr/settings.json` → `mcp_servers`.

```json
{
  "mcp_servers": {
    "az-retrieval-mcp": {
      "command": "python3",
      "args": ["/a0/usr/mcp/server.py"],
      "env": {
        "DATA_LAYER_POSTGRES_DSN": "postgresql://postgres@localhost:5432/postgres"
      },
      "timeout": 10
    }
  }
}
```

- The `az-retrieval-mcp` alias is the canonical global identifier. `command = python3` runs the deployed copy at `/a0/usr/mcp/server.py` (source lives at `/a0/usr/projects/data-layer/data-layer-adapters/mcp/server.py`; deployment is via the umbrella `bootstrap` which `rsync`s the source into the live path).
- `env.DATA_LAYER_POSTGRES_DSN` overrides the in-code default `postgresql://postgres@localhost:5432/postgres` (see `mcp/server.py:38-41`).
- `timeout = 10` is the per-call deadline; matches `QDRANT_HTTP_TIMEOUT_S` in `mcp/tools/rag.py:59` and gives the MCP a 10s wall-clock budget per `tools/call`.
- This registration is the **only** one the runtime honours. It is read at framework boot.

### 7.2 Per-project registration history (deduped)

Until 2026-09-16T16:41:42Z, every `.a0proj/mcp_servers.json` in the umbrella + every submodule carried its own copy of the registration (same `az-retrieval-mcp` block). On that timestamp all six files (umbrella + 5 submodules) were **deduped to `{}`**; the previous content was preserved at `.bak-20260916T164142Z-mcp-dedupe` alongside each.

The dedupe rationale:

1. **Single source of truth.** The framework-global `/a0/usr/settings.json` is read at framework boot. A second per-project copy is silently shadowed, which is worse than absent: it creates the illusion of configuration authority that does not exist.
2. **No per-project override needed.** Every submodule (postgres/redis/falkordb/qdrant/adapters) reads the same `DATA_LAYER_POSTGRES_DSN` from the env block above. There is no per-submodule DSN that would justify a per-project registration.
3. **Removes a footgun.** A future operator editing `.a0proj/mcp_servers.json` in any submodule would have believed the change took effect — it would not have. The dedupe forecloses that failure mode.
4. **Bootstrap can re-create if needed.** If a future per-submodule override becomes necessary, the registration belongs back in `.a0proj/mcp_servers.json` (with a per-submodule `env` block). Until then, the global file is canonical.

Backup example (umbrella, preserved):

```json
{
  "mcpServers": {
    "az-retrieval-mcp": {
      "args": ["/a0/usr/mcp/server.py"],
      "command": "python3",
      "env": {
        "DATA_LAYER_POSTGRES_DSN": "postgresql://postgres@localhost:5432/postgres"
      }
    }
  }
}
```

(Note: the backup uses `mcpServers` rather than `mcp_servers` — historical naming inconsistency. The global file uses `mcp_servers`, which is the schema the framework reads.)

The per-project files in this submodule after dedupe:

```json
{}
```

(i.e., `.a0proj/mcp_servers.json` is a one-line empty object.)

### 7.3 Tool registration flow at runtime

When Agent Zero starts the MCP via the global registration:

1. `python3 /a0/usr/mcp/server.py` is launched with stdin/stdout JSON-RPC.
2. `server.py` reads `DATA_LAYER_POSTGRES_DSN` from env (inherited from the global `mcp_servers` block).
3. On `initialize`, returns `protocolVersion: 2024-11-05`, `serverInfo: {name: data-layer, version: 0.3.0}`.
4. On `tools/list`, merges `TOOL_DESCRIPTORS` (14 postgres) + `TOOL_DESCRIPTORS_RAG` (7 Qdrant) + `FRAMEWORK_DESCRIPTORS` (empty) = 21 tools.
5. On `tools/call`, dispatches `name` through `TOOL_REGISTRY` ∪ `TOOL_REGISTRY_RAG`. Errors caught and returned as `{"error":{"code":-32000,"message":...}}`.

---

## 8. Cross-links

| Doc | Purpose |
|---|---|
| [`docs/SUBMODULE_OWNERSHIP.md`](../../docs/SUBMODULE_OWNERSHIP.md) | Wiring-route map; canonical reference for who-calls-whom; boundary rules #1+4. |
| [`data-layer-postgres/SCHEMAS.md`](../data-layer-postgres/SCHEMAS.md) | Column-by-column schema for the 13 tables touched by the 14 postgres-backed MCP tools. |
| [`data-layer-qdrant/SCHEMAS.md`](../data-layer-qdrant/SCHEMAS.md) | Payload schemas for the two Qdrant collections touched by the 7 `rag.*` tools. |
| [`data-layer-redis/SCHEMAS.md`](../data-layer-redis/SCHEMAS.md) | Key namespace + TTL contract for the cache half of the dual-write path. |
| [`data-layer-falkordb/SCHEMAS.md`](../data-layer-falkordb/SCHEMAS.md) | Node labels + edge types projected by `lib/redis_publish_hook.py`. |
| [`../../TOOLS_AND_WIRING.md`](../../TOOLS_AND_WIRING.md) | Umbrella-level wiring (orchestration, install.sh, docker-compose, MCP registration). |
| [`docs/decisions/0001-dual-write-and-redis-publish-hook.md`](decisions/0001-dual-write-and-redis-publish-hook.md) | ADR for the dual-write path; failure containment rules. |
| [`docs/decisions/0002-phase-3-cutover-procedure.md`](decisions/0002-phase-3-cutover-procedure.md) | Per-category flag cutover procedure for `write_through.py`. |
| [`docs/decisions/0003-phase-4-observability.md`](decisions/0003-phase-4-observability.md) | `_Counters` snapshot export design. |

---

## 9. Schema-authority footer

**`mcp/server.py` is authoritative for tool behaviour.** This document mirrors it line-for-line; tool count (21), gating (`MCP_INSTALL_MODE=1`, SOT invariant, SELECT-only), dispatch order, and return shapes are pulled directly from `mcp/server.py`, `mcp/tools/retrieval.py`, `mcp/tools/rag.py`, and `mcp/tools/safety.py`. `lib/write_through.py` is authoritative for dual-write sequence; `lib/redis_publish_hook.py` is authoritative for the falkordb projection path. The framework adapters (`agent-zero/`, `hermes-agent/`) are authoritative for their seed contracts.

Any change that adds a tool, changes a tool's gating, promotes a `rag.ingest.*` to runtime (re-spec boundary rule #4 per `SUBMODULE_OWNERSHIP.md`), adds a dual-write category, or adds a framework adapter **must** update this file in the same change.
