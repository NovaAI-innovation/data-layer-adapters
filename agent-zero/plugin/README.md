# Data Management Plugin

Local-only Agent Zero plugin that exposes a single SQL execution tool against a configurable Postgres database. Built for the persistence-mcp-sprint to run migration scripts and dry-runs.

## What it does

Adds one tool, `execute_sql`, that the agent can call to:

- Read rows from any table (`SELECT`, `WITH`)
- Run schema migrations (`CREATE TABLE`, `INSERT`, etc.) with explicit opt-in
- Run destructive operations (`DROP`, `TRUNCATE`, `ALTER`) only with explicit `force=true`

The DSN is read from plugin configuration; the tool refuses to run if it isn't set.

## Safety

The plugin is **read-only by default**. To run any write statement, the caller must pass `readonly=False`. To run a verb in the `require_force_for` list (default: `DROP`, `TRUNCATE`, `ALTER`), the caller must additionally pass `force=True`.

Other guards:

- `SET statement_timeout` is applied per connection (default 30s, configurable).
- `SET TRANSACTION READ ONLY` is issued on the connection when `readonly=True`, so even direct SQL injection at the cursor level can't mutate.
- All writes happen inside a transaction; failures roll back automatically.
- Result sets are capped at `select_row_limit` rows (default 1000); larger SELECTs are truncated and reported.

## Configuration

Edit `default_config.yaml` or override per-project via Settings → Agent → Data Management.

| Key | Default | Meaning |
|---|---|---|
| `dsn` | `""` | Postgres connection string. Required. |
| `pool_min` | 1 | Min pool connections. |
| `pool_max` | 5 | Max pool connections. |
| `statement_timeout_ms` | 30000 | Per-statement timeout. |
| `readonly_default` | true | Default mode for the tool. |
| `require_force_for` | `[DROP, TRUNCATE, ALTER]` | Verbs that need `force=true`. |
| `select_row_limit` | 1000 | Max rows returned per SELECT. |

## Tool

### `execute_sql`

**Parameters:**

| Name | Type | Default | Notes |
|---|---|---|---|
| `sql` | string | required | The SQL to execute. |
| `readonly` | bool | `true` | Set `false` for writes. |
| `force` | bool | `false` | Required for verbs in `require_force_for`. |

**Returns:**

- For `SELECT`: a formatted table with columns, rows (up to `select_row_limit`), and a row count.
- For writes: `OK · N row(s) affected · committed`.
- On safety rejection: `data_management: <reason>`.
- On SQL error: `data_management: SQL execution failed: <reason>`.

## Examples

```
# Count rows
execute_sql(sql="SELECT count(*) AS n FROM conversations")

# Inspect a sample
execute_sql(sql="SELECT id, title, status FROM conversations ORDER BY created_at DESC LIMIT 5")

# Run a migration
execute_sql(
    sql="BEGIN; CREATE TABLE projects (id uuid PRIMARY KEY, project_key text UNIQUE NOT NULL); COMMIT;",
    readonly=False
)

# Destructive op
execute_sql(sql="DROP TABLE foo", readonly=False, force=True)
```

## Dependency

The plugin uses `psycopg` (v3) — the modern Postgres driver. Install in the framework runtime:

```bash
/opt/venv-a0/bin/pip install 'psycopg[binary]>=3.1'
```

The plugin's `hooks.py::install` will warn at load time if it's missing.

## Files

```
/usr/plugins/data_management/
├── plugin.yaml              # manifest
├── default_config.yaml      # default settings
├── hooks.py                 # install / pre_update / uninstall hooks
├── helpers/
│   └── db.py                # connection pool + safety + execution
├── tools/
│   └── execute_sql.py       # the agent-callable tool
└── README.md
```

## Persistence-mcp-sprint usage

This plugin was built to unblock migration dry-runs for the new 10-table schema:

1. Configure `data_management.dsn` with the sbzm Postgres connection string.
2. Run `0001_init.sql` via `execute_sql` to create the new tables (read-only OFF, force ON for the destructive-grant needs of any subsequent DROP).
3. Run `0002_backfill.sql` to populate from the legacy 3-table layout.
4. Compare row counts before/after to validate the migration.

The schema migration files live at:

- `/a0/usr/workdir/persistence-mcp-sprint/migrations/0001_init.sql`
- `/a0/usr/workdir/persistence-mcp-sprint/migrations/0002_backfill.sql`
