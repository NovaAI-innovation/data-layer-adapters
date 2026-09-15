"""Connection pool and SQL execution helpers for the data_management plugin.

Pure Python — no Agent Zero imports here. The tool layer in
`tools/execute_sql.py` wraps these functions and adapts the
return shape to the framework.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

try:
    import psycopg
    from psycopg import sql as pg_sql
    _HAS_PSYCOPG = True
except ImportError:  # pragma: no cover
    _HAS_PSYCOPG = False


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

@contextmanager
def get_connection(
    dsn: str,
    *,
    statement_timeout_ms: int = 30000,
    readonly: bool = False,
) -> Iterator["psycopg.Connection"]:
    """Yield a single connection with statement_timeout applied.

    The caller controls transaction scope. Commits/rollbacks happen in
    execute_sql() or the caller.
    """
    if not _HAS_PSYCOPG:
        raise RuntimeError(
            "psycopg is not installed. pip install 'psycopg[binary]' in the "
            "agent runtime (typically /opt/venv-a0 or /opt/venv)."
        )
    if not dsn:
        raise ValueError("DSN is empty. Configure data_management.dsn first.")

    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
            if readonly:
                cur.execute("SET TRANSACTION READ ONLY")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise


# ---------------------------------------------------------------------------
# SQL safety: detect destructive operations
# ---------------------------------------------------------------------------

_LEADING_COMMENT_RE = None

import re


def _first_keyword(sql: str) -> str:
    """Return the first SQL keyword in `sql`, ignoring leading whitespace and comments."""
    s = sql.strip()
    while s:
        if s.startswith("--"):
            nl = s.find("\n")
            if nl == -1:
                return ""
            s = s[nl + 1:].lstrip()
        elif s.startswith("/*"):
            end = s.find("*/")
            if end == -1:
                return ""
            s = s[end + 2:].lstrip()
        else:
            break
    if not s:
        return ""
    head = s.split(None, 1)[0]
    return head.upper().rstrip(";")


def _is_write(sql: str) -> bool:
    """Return True if the first keyword suggests a write operation."""
    kw = _first_keyword(sql)
    return kw in {
        "INSERT", "UPDATE", "DELETE", "MERGE", "UPSERT",
        "CREATE", "DROP", "ALTER", "TRUNCATE", "GRANT", "REVOKE",
    }


def check_safety(
    sql: str,
    *,
    readonly: bool,
    force: bool,
    require_force_for: list[str],
) -> None:
    """Raise PermissionError if the statement is unsafe for the current mode."""
    verb = _first_keyword(sql)

    if verb in (kw.upper() for kw in require_force_for) and not force:
        raise PermissionError(
            f"Refusing to execute {verb} without force=true. "
            f"Pass force=true if you really mean it."
        )

    if readonly and _is_write(sql):
        raise PermissionError(
            f"Refusing to execute {verb} statement in readonly mode. "
            f"Pass readonly=false to opt into writes."
        )


# ---------------------------------------------------------------------------
# Main execution helper
# ---------------------------------------------------------------------------

def execute_sql(
    dsn: str,
    sql: str,
    *,
    readonly: bool = True,
    force: bool = False,
    require_force_for: list[str] | None = None,
    select_row_limit: int = 1000,
    statement_timeout_ms: int = 30000,
) -> dict[str, Any]:
    """Execute a single SQL statement and return a structured result.

    Args:
        dsn: Postgres connection string.
        sql: The SQL to execute (single statement; multiple statements
            separated by `;` are all run in the same transaction).
        readonly: If True, refuses anything other than SELECT/WITH and
            uses SET TRANSACTION READ ONLY at the connection level.
        force: Required to execute verbs in require_force_for, even
            when readonly is False.
        require_force_for: List of verbs that require force=true
            (case-insensitive). Example: ["DROP", "TRUNCATE", "ALTER"].
        select_row_limit: Maximum rows returned for SELECTs; further
            rows are truncated and reported.
        statement_timeout_ms: Postgres statement_timeout for this
            statement.

    Returns:
        For SELECTs:
            {
                "kind": "select",
                "columns": [...],
                "rows": [[...], ...],
                "row_count": N,
                "truncated": bool,
            }
        For writes:
            {
                "kind": "write",
                "affected_rows": N,
                "status": "committed",
            }

    Raises:
        PermissionError: if safety checks fail.
        Exception: any underlying psycopg/Postgres error after rollback.
    """
    require_force_for = require_force_for or ["DROP", "TRUNCATE", "ALTER"]
    check_safety(
        sql,
        readonly=readonly,
        force=force,
        require_force_for=require_force_for,
    )

    with get_connection(
        dsn,
        statement_timeout_ms=statement_timeout_ms,
        readonly=readonly,
    ) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                if cur.description:
                    cols = [d.name for d in cur.description]
                    rows = cur.fetchmany(select_row_limit)
                    truncated = len(rows) == select_row_limit
                    return {
                        "kind": "select",
                        "columns": cols,
                        "rows": [list(r) for r in rows],
                        "row_count": len(rows),
                        "truncated": truncated,
                    }
                affected = cur.rowcount
            conn.commit()
            return {
                "kind": "write",
                "affected_rows": affected,
                "status": "committed",
            }
        except Exception:
            conn.rollback()
            raise


def format_result(result: dict[str, Any]) -> str:
    """Render the dict returned by execute_sql() as a human-readable string."""
    if result["kind"] == "select":
        cols = result["columns"]
        rows = result["rows"]
        # Compute column widths
        widths = [max(len(c), 3) for c in cols]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(str(cell) if cell is not None else "NULL"))

        def fmt_row(row: list) -> str:
            cells = []
            for i, cell in enumerate(row):
                s = str(cell) if cell is not None else "NULL"
                cells.append(s.ljust(widths[i]))
            return " | ".join(cells)

        header = fmt_row(cols)
        sep = "-+-".join("-" * w for w in widths)
        body = "\n".join(fmt_row(r) for r in rows)
        trunc = "\n... [truncated]" if result["truncated"] else ""
        return f"{header}\n{sep}\n{body}{trunc}\n\n({result['row_count']} row(s))"
    return f"OK · {result['affected_rows']} row(s) affected · {result['status']}"