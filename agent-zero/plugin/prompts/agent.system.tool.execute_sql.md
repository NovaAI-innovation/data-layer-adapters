### execute_sql:
run a single SQL statement against the configured Postgres database.
use only when the user explicitly asks you to query, inspect, migrate, or modify the database. do not invent queries against data the user has not asked about.
default mode is read-only. any write statement is refused unless `readonly` is explicitly false. statements whose leading verb is in `require_force_for` (default DROP, TRUNCATE, ALTER) are refused unless `force` is true. failures raise a clear error — never silently fall back.
args:
- `sql`: string. the SQL to execute. one transaction per call. multiple statements separated by `;` all run in the same transaction.
- `readonly`: bool default true. set false to allow INSERT/UPDATE/DELETE/CREATE/MERGE/GRANT/REVOKE.
- `force`: bool default false. required for verbs in `require_force_for`. combine with `readonly=false` for destructive ops.
prerequisites:
- the plugin config `data_management.dsn` must be set (Settings → Agent → Data Management). the tool returns a friendly error if it isn't — do not try to set it from the agent.
- psycopg must be installed in the runtime that runs the tool (typically `/opt/venv`). tell the user to install with `pip install 'psycopg[binary]>=3.1'` if the tool fails with an import error.
output:
- SELECT: a formatted table with columns and up to `select_row_limit` rows (default 1000); truncation is flagged.
- write: `OK · N row(s) affected · committed`.
- safety refusal: `data_management: <reason>`. do not retry without addressing the reason.
- error: `data_management: SQL execution failed: <reason>`.
usage:
~~~json
{
    "thoughts": ["User asked for row counts across the three legacy tables; safe to run as SELECT."],
    "headline": "Counting legacy tables",
    "tool_name": "execute_sql",
    "tool_args": {
        "sql": "SELECT 'conversations' AS t, count(*) FROM conversations UNION ALL SELECT 'messages', count(*) FROM messages UNION ALL SELECT 'tool_calls', count(*) FROM tool_calls;"
    }
}
~~~
~~~json
{
    "thoughts": ["Running the 0001_init migration; needs write mode and is a CREATE so force is not required (CREATE is not in require_force_for)."],
    "headline": "Applying schema migration",
    "tool_name": "execute_sql",
    "tool_args": {
        "sql": "BEGIN; CREATE TABLE projects (id uuid PRIMARY KEY DEFAULT uuid_generate_v4(), project_key text UNIQUE NOT NULL, display_name text NOT NULL); COMMIT;",
        "readonly": false
    }
}
~~~
~~~json
{
    "thoughts": ["User explicitly asked for the legacy messages table to be dropped; this is destructive so force is required."],
    "headline": "Dropping legacy table",
    "tool_name": "execute_sql",
    "tool_args": {
        "sql": "DROP TABLE messages",
        "readonly": false,
        "force": true
    }
}
~~~
for long existing text, use `§§include(path)` instead of rewriting