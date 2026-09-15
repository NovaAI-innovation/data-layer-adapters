# data-layer-adapters

Multi-framework adapter collection for the data-layer stack.

This project owns framework-specific code (plugin glue, prompts, hooks) and
framework-specific seed rows. It is framework-agnostic in the sense that
the postgres schema lives elsewhere (`../data-layer-postgres/`) and the
MCP server is universal (`mcp/`).

## Layout

```
data-layer-adapters/
├── agent-zero/                  # Agent Zero framework family (Jan Tomasek)
│   ├── bootstrap                # install | verify | status | reset | seed
│   ├── lib/                     # A0-specific install scripts
│   ├── plugin/                  # A0 plugin code (data_management etc.)
│   ├── prompts/                 # A0-specific prompt overrides
│   └── seeds/                   # A0 DB seed rows (idempotent)
├── hermes-agent/                # Hermes framework family (Nous Research)
│   ├── bootstrap                # same shape as agent-zero
│   ├── lib/, plugin/, prompts/, seeds/
├── mcp/                         # UNIVERSAL MCP server (read-only, framework-agnostic)
│   ├── server.py                # JSON-RPC dispatcher + 21 tool descriptors
│   ├── README.md                # tool catalogue + governance policy
│   ├── tools/                   # retrieval (14 PG), rag (7 Qdrant), safety, _registry
│   └── tests/                   # unit + integration smoke tests (31 tests)
```

> **MVP framework set:** `agent-zero` and `hermes-agent` only. No
> additional framework adapters are planned. Adding a new framework
> requires a new ADR.

## Per-adapter commands

```bash
bash agent-zero/bootstrap seed       # apply A0 seed rows
bash hermes-agent/bootstrap seed     # apply Hermes seed rows
bash mcp/server.py                  # run the universal MCP server (stdio)
```

## Universal MCP (read-only)

The MCP server in `mcp/` speaks the MCP protocol (`2024-11-05`). It
is not coupled to any framework. Tools are addressed by name;
per-framework tool variants live under `mcp/tools/<framework>/`
when needed but the server itself is universal.

**Governance: read-only. Zero write tools exposed.** The agent has
no authority or autonomy over DB writes. Per-project writes happen
via the governed bootstrap scripts in `agent-zero/bootstrap` and
`hermes-agent/bootstrap`, which run deterministic SQL via
`psql -v ON_ERROR_STOP=1` against files in `seeds/`. The DB is the
SOT and is managed by a governed documentation system, not by the
agent's judgement.

The 21 tools (see `mcp/README.md` for full catalogue):

| Group | Tools |
|---|---|
| Health & lists | `health.check`, `projects.list`, `agents.list`, `sessions.list`, `messages.list`, `tool_executions.list` |
| Single-row gets | `projects.get`, `agents.get`, `sessions.get`, `messages.get`, `tool_executions.get` |
| Search & composite | `messages.search`, `history.retrieve` |
| Raw SQL (SELECT-only) | `execute_sql` — refuses 17 write verbs at the Python layer before opening any cursor |
| **RAG reads (Qdrant-backed)** | `rag.health`, `rag.collections.list`, `rag.collection.info`, `rag.search`, `rag.ingest.status` |
| **RAG writes (gated)** | `rag.ingest.point`, `rag.ingest.batch` — refuse unless `MCP_INSTALL_MODE=1` is set in the env of the process that spawned the MCP |

The two `rag.ingest.*` writes are the only writes the MCP ever exposes,
and they are strictly gated to bootstrap-script use. All other writes
to the data layer still happen via the `bootstrap` scripts under
`agent-zero/` and `hermes-agent/`. The qdrant layer itself lives in
the sibling submodule `../data-layer-qdrant/` (5th submodule of the
umbrella); this MCP is the single agent-facing surface that talks to
both Postgres and Qdrant.

### Removed (vs the prior 2-tool server)

- `session.heartbeat` — runtime write tool; not install-only; the
  dual-write lib at `lib/write_through.py` still fires from
  `lib/redis_publish_hook.py` via Postgres `NOTIFY` (not via MCP).
  The per-framework `mcp/tools/<framework>/session_presence.py`
  wrappers are dead code; banners added; deletion is a follow-up ADR.
- The unrestricted `execute_sql` write path — replaced with a
  strictly SELECT-only structural guard. No `readonly` / `force`
  / `require_force_for` flags; no escape hatch.

### Smoke tests

```bash
cd data-layer-adapters
python3 -m unittest mcp.tests.test_server -v
```

25 structural + protocol tests + 3 RAG-against-live-Qdrant tests run
without a DB; 2 DB-gated Postgres tests + 1 subprocess-handshake test
skip cleanly when `DATA_LAYER_TEST_DSN` is unset or unreachable.
