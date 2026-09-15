"""SQL execution tool for the data_management plugin.

The agent calls this tool with a SQL string and optional safety flags.
The tool delegates to `helpers.db.execute_sql` for the actual work and
formats the result for the agent's display.

Usage by the agent:
    execute_sql(sql="SELECT count(*) FROM conversations")
    execute_sql(sql="BEGIN; CREATE TABLE ...; COMMIT;", readonly=False)
    execute_sql(sql="DROP TABLE foo", readonly=False, force=True)
"""

from __future__ import annotations

from typing import Any

from helpers.tool import Response, Tool
from helpers.plugins import get_plugin_config

from usr.plugins.data_management.helpers.db import (
    execute_sql as _execute_sql_impl,
    format_result as _format_result,
)


class ExecuteSqlTool(Tool):
    """Execute a SQL statement against the configured Postgres DSN.

    The DSN is read from plugin configuration (`data_management.dsn`).
    If not configured, the tool returns a clear error message and does
    not attempt to connect.

    Safety:
      - Default mode is read-only (`readonly=True`). The tool refuses any
        write statement in this mode.
      - Pass `readonly=False` to enable writes (INSERT, UPDATE, DELETE,
        CREATE, MERGE, GRANT, REVOKE).
      - Statements matching the `require_force_for` config list (default
        `DROP`, `TRUNCATE`, `ALTER`) additionally require `force=True`.

    Results:
      - SELECT returns rows + columns, truncated to
        `select_row_limit` (default 1000).
      - Writes return `affected_rows` and `status: "committed"`.
      - Errors return the underlying Postgres message.
    """

    async def execute(
        self,
        sql: str,
        readonly: bool = True,
        force: bool = False,
    ) -> Response:
        import os
        cfg = get_plugin_config("data_management") or {}
        dsn = cfg.get("dsn", "").strip()
        if not dsn:
            # Fallback to env var so the tool works without explicit
            # Settings UI configuration. The runtime expands secret
            # aliases on read, so this returns the resolved DSN.
            dsn = os.environ.get("EVENT_LEDGER_DSN", "").strip()
        if not dsn:
            return Response(
                message=(
                    "data_management: dsn is not configured. Set it in "
                    "Settings → Agent → Data Management, or via the "
                    "project's config.json."
                ),
                break_loop=False,
            )

        try:
            result = _execute_sql_impl(
                dsn=dsn,
                sql=sql,
                readonly=readonly,
                force=force,
                require_force_for=cfg.get(
                    "require_force_for", ["DROP", "TRUNCATE", "ALTER"]
                ),
                select_row_limit=int(cfg.get("select_row_limit", 1000)),
                statement_timeout_ms=int(
                    cfg.get("statement_timeout_ms", 30000)
                ),
            )
            return Response(
                message=_format_result(result),
                break_loop=False,
            )
        except PermissionError as e:
            return Response(message=f"data_management: {e}", break_loop=False)
        except Exception as e:
            return Response(
                message=f"data_management: SQL execution failed: {e}",
                break_loop=False,
            )