# Data-Layer MCP Server (Universal, Read-Only)

This directory contains the **universal MCP server** for the Agent
Zero data layer. It speaks the [Model Context Protocol](https://modelcontextprotocol.io/)
(JSON-RPC over stdio, protocolVersion `2024-11-05`) and is **not**
coupled to any specific framework — both Agent Zero and Hermes agents
talk to it through the same tool surface.

## Governance policy — read-only by default

**Zero write tools.** The agent has no authority or autonomy to mutate
the data layer. All DB writes happen via the **governed bootstrap
scripts** that live next to this directory:

```
data-layer-adapters/
├── agent-zero/bootstrap    # install | verify | status | reset | seed
├── hermes-agent/bootstrap  # install | verify | status | reset | seed
└── mcp/                    # ← this directory (read-only retrieval)
```

`bash agent-zero/bootstrap seed` (and the hermes equivalent) run
deterministic SQL files via `psql -v ON_ERROR_STOP=1` against the
schema in `../data-layer-postgres/migrations/`. Those scripts are
version-controlled, idempotent, and operator-driven. The agent cannot
invoke them.

The rationale is simple: **the DB is a Source of Truth managed by a
governed documentation system, not by the autonomy or judgement of the
agent.** A free-rein write tool would let the agent invent writes
that diverge from the documented schema migrations, so we removed all
write tools from the MCP.

If you genuinely need a write during install/seed, you do it via the
bootstrap scripts, not via this MCP.

## Tool catalogue — 14 read-only tools

All 14 tools return JSON. None accept a flag that switches them into a
write mode. The `execute_sql` tool is structurally restricted to
SELECT/WITH/VALUES/EXPLAIN/SHOW at the Python level before the cursor
is opened (see `tools/safety.py`).

| Tool                        | Reads from                       | Notes |
|-----------------------------|----------------------------------|-------|
| `health.check`              | `pg_catalog`, all 10 tables      | DSN probe + per-table row counts |
| `projects.list`             | `projects`                       | filter: `status` |
| `projects.get`              | `projects`                       | by `project_key` |
| `agents.list`               | `agents` ∪ `agent_frameworks`    | filter: `project_key`, `framework`, `status` |
| `agents.get`                | `agents`                         | by `id` (uuid) |
| `sessions.list`             | `sessions`                       | filter: `agent_id`, `status`, `since`, `until` |
| `sessions.get`              | `sessions`                       | by `id` (uuid) |
| `messages.list`             | `messages`                       | filter: `session_id`, `agent_id`, `role`, `since`, `until` |
| `messages.get`              | `messages`                       | by `id` (uuid), full content returned |
| `messages.search`           | `messages.content`               | ILIKE search with filters; ranking by recency |
| `tool_executions.list`      | `tool_executions`                | filter: `session_id`, `agent_id`, `tool_name`, `status`, `since`, `until` |
| `tool_executions.get`       | `tool_executions`                | by `id` (uuid), full payload returned |
| `history.retrieve`          | `messages` ∪ `tool_executions`   | composite free-text + filters; ranked, cross-table |
| `execute_sql`               | any (read-only)                  | **strictly SELECT-only** — refuses 17 write verbs before opening the cursor |

### What `execute_sql` refuses

At the Python level, before any DB connection, `execute_sql` rejects
the following leading verbs (case-insensitive, after comment stripping):

`INSERT UPDATE DELETE DROP CREATE ALTER TRUNCATE GRANT REVOKE MERGE
CALL COPY LOCK VACUUM REINDEX CLUSTER REFRESH CHECKPOINT`

That covers the 17 write verbs the framework is likely to encounter on
the data-layer. Anything else (e.g. `FOOBAR`) is also refused with an
unrecognized-token error.

Allowed leading verbs: `SELECT WITH VALUES EXPLAIN SHOW`.

## Configuration

| Env var                     | Purpose                                          | Default                                  |
|-----------------------------|--------------------------------------------------|------------------------------------------|
| `DATA_LAYER_POSTGRES_DSN`   | psycopg DSN for the live schema                  | `postgresql://postgres@localhost:5432/postgres` (dev only) |

Production deployments MUST set `DATA_LAYER_POSTGRES_DSN` explicitly.
The default fallback exists for in-process tests against a local
postgres and is not safe for multi-tenant usage.

## Quick start

### Spawn the server (stdio MCP)

```bash
cd data-layer-adapters
python3 mcp/server.py
```

The server reads JSON-RPC requests from stdin and emits responses to
stdout (one JSON object per line). Spawn it from a parent MCP client
(MCP-Python, mcp-curl, etc.).

### Smoke test (no DB needed)

```bash
cd data-layer-adapters
python3 -m unittest mcp.tests.test_server -v
```

Runs 12 structural + protocol tests + 2 DB-gated tests (the DB-gated
ones skip cleanly if `DATA_LAYER_TEST_DSN` or `DATA_LAYER_POSTGRES_DSN`
is unset or unreachable).

### Manual wire-protocol test

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize"}'   | python3 mcp/server.py
echo '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'     | python3 mcp/server.py
echo '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"health.check","arguments":{}}}' | python3 mcp/server.py
```

The last one returns `ok: true` plus a per-table row-count map if the
DSN is reachable.

## Schema reference (10 tables)

The tools operate on the schema created by `../data-layer-postgres/migrations/`:

`projects, agent_frameworks, agents, agent_skills, agent_plugins,
available_tools, hooks, sessions, messages, tool_executions`

See `../data-layer-postgres/migrations/0001_init.sql` for column
definitions.

## File layout

```
mcp/
├── server.py                 # JSON-RPC dispatcher + 14 tool descriptors
├── README.md                 # this file
├── tools/
│   ├── __init__.py           # package marker
│   ├── safety.py             # SELECT-only guard, statement_timeout, row_limit_cap
│   ├── retrieval.py          # 14 read-only tool implementations + TOOL_REGISTRY
│   ├── _registry.py          # per-framework descriptor extension point (currently empty)
│   ├── agent-zero/           # deprecated heartbeat wrapper (banner added; deletion is a follow-up ADR)
│   └── hermes-agent/         # deprecated heartbeat wrapper (banner added; deletion is a follow-up ADR)
└── tests/
    ├── __init__.py           # test package marker
    └── test_server.py        # 14 unit + integration tests
```

## How to add a tool

1. Open `tools/retrieval.py` and add a `_tool_<name>(args, dsn)` function.
   - Must not write to the DB.
   - Use parameterized queries (`%s` placeholders + tuple of params).
   - Use `apply_session_limits(cur)` for statement_timeout.
   - Use `safety.jsonify` to make UUID/datetime/Decimal/bytes JSON-safe.
   - Use `_bounded_limit(args, default, hard_max)` for limit clamping.
   - Return `{"result": ...}` or `{"error": {"code": ..., "message": ...}}`.
2. Register the function in `TOOL_REGISTRY` at the bottom of `retrieval.py`.
3. Add a matching descriptor entry in `server.py`'s `TOOL_DESCRIPTORS` list
   (name, description, inputSchema). Keep them in sync — the smoke test
   fails if they drift.
4. Add a unit test in `tests/test_server.py`.
5. Run `python3 -m unittest mcp.tests.test_server -v` — all tests must
   pass before the change is considered complete.

## Per-framework tool extension point

`tools/_registry.py` exposes `FRAMEWORK_DESCRIPTORS: list`. Today it
is empty. To add a per-framework tool (e.g. `agent-zero.foo`):

1. Drop a file under `tools/<framework>/foo.py` with a
   `mcp_tool_descriptor() -> dict` function.
2. Import it in `tools/_registry.py` and append its descriptor to
   `FRAMEWORK_DESCRIPTORS`.
3. Implement the function (call into the universal `retrieval.py` if
   the logic is framework-agnostic; otherwise keep it framework-
   specific).
4. Add a unit test.

Per-framework tools must still be **read-only** unless they fit the
explicit install/seed exception (today, none do — install/seed is the
bootstrap scripts' job, not the MCP's).

## Security notes

- The MCP speaks over a JSON-RPC stdio loop. There is no HTTP surface.
  Wrap it in your MCP client's stdio transport; do not bind it to a
  public TCP port without going through the framework's hardened MCP
  host (`/a0/helpers/mcp_server.py`) and its host-allowlist /
  CORS configuration.
- DSN credentials travel in `DATA_LAYER_POSTGRES_DSN`. Treat that env
  var like a database password. Don't log it.
- `execute_sql` is structurally guarded, but a sophisticated caller
  could route writes via plpgsql (e.g. `SELECT my_dml_func()`). The
  data-layer DSN is intended for an internal-only network; do not
  expose it beyond the trust boundary.
- The structural guard **fails closed**: an unparseable statement is
  refused. This is intentional.

## Why no vector embeddings / semantic search?

`messages.search` and `history.retrieve` use ILIKE on content. That
covers the common case ("find me messages mentioning 'X'") without
adding pgvector + embedding-model dependencies. If you later want
semantic retrieval:

1. Apply migration `0003_pgvector.sql` (already present in the project).
2. Add an extension point in `tools/retrieval.py` for a `q` that
   routes to `messages.embedding <=> %s`.
3. Add a separate tool (e.g. `messages.semantic_search`) so the
   existing ILIKE-based tools stay available.

This is deferred to a follow-up ADR to keep this build's scope tight.
