"""Safety helpers for the universal data-layer MCP server.

The MCP is purely read-only by governance. This module enforces:

1. ``execute_sql`` only accepts statements whose leading verb is
   SELECT / WITH / VALUES / EXPLAIN / SHOW (case-insensitive, after
   stripping SQL comments and leading whitespace). Any other verb
   (INSERT / UPDATE / DELETE / DROP / CREATE / ALTER / TRUNCATE /
   GRANT / REVOKE / MERGE / CALL / COPY / LOCK / VACUUM / REINDEX /
   CLUSTER / REFRESH / CHECKPOINT) returns an MCP error before the
   cursor is opened.

2. Each SELECT runs with a per-transaction ``statement_timeout``
   (default 30s) via ``SET LOCAL``, and the result is trimmed to a
   row cap (default 1000) before returning.

3. Every result row is converted to a JSON-safe structure
   (UUID → str, datetime → ISO 8601, Decimal → float, bytes → utf-8)
   so that JSON-RPC serializes cleanly.

This module imports only stdlib + psycopg. It does NOT import
anything from the data_management plugin; the plugin will be
deprecated once this MCP is verified.
"""
from __future__ import annotations

import datetime
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

# Default limits. Conservative defaults; lowering is a future change.
DEFAULT_STATEMENT_TIMEOUT_MS = 30_000
DEFAULT_SELECT_ROW_LIMIT = 1_000

# Verbs we refuse anywhere in the statement. Word-boundary so SELECT
# (which we allow) does not match SELECT_INTO etc.
_WRITE_VERBS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
    "TRUNCATE", "GRANT", "REVOKE", "MERGE", "CALL",
    "COPY", "LOCK", "VACUUM", "REINDEX", "CLUSTER",
    "REFRESH", "CHECKPOINT",
)

_ALLOWED_LEAD_TOKENS = frozenset({"SELECT", "WITH", "VALUES", "EXPLAIN", "SHOW"})

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE = re.compile(r"^\s*--.*$", re.MULTILINE)
_WHITESPACE = re.compile(r"\s+")


def strip_sql_comments(sql: str) -> str:
    """Strip SQL line and block comments; collapse whitespace."""
    s = _COMMENT_BLOCK.sub(" ", sql)
    s = _COMMENT_LINE.sub(" ", s)
    return s


def _first_token(sql: str) -> Optional[str]:
    """Return the first SQL keyword token, or None if the input is empty."""
    s = strip_sql_comments(sql).strip()
    if not s:
        return None
    parts = _WHITESPACE.split(s, maxsplit=1)
    return parts[0].upper() if parts else None


def assert_select_only(sql: str) -> Optional[Dict[str, Any]]:
    """Return None if ``sql`` is a read-only verb, else an error dict.

    The check is structural (text-level); it does NOT require a DB
    roundtrip and does NOT consult the catalog. It is designed to fail
    closed against the common write verbs. Note: a malicious caller
    could route writes via plpgsql functions (e.g., ``SELECT
    my_dml_func()``); the data-layer DSN is intended for an
    internal-only network, and that risk is documented in the README.
    """
    first = _first_token(sql)
    if first is None:
        return _err("empty SQL")
    if first in _ALLOWED_LEAD_TOKENS:
        return None
    if first in _WRITE_VERBS:
        return _err(
            f"refused: '{first}' is not a read-only verb. This MCP "
            "exposes only SELECT-level read access. Writes must be "
            "performed by the governed bootstrap scripts (bash "
            "<adapter>/bootstrap seed) or operator-driven psql, not "
            "by this MCP."
        )
    return _err(
        f"refused: unrecognized leading token '{first}'. This MCP "
        "accepts only SELECT/WITH/VALUES/EXPLAIN/SHOW."
    )


def _err(msg: str) -> Dict[str, Any]:
    return {
        "error": {
            "code": -32000,
            "message": f"data-layer MCP safety: {msg}",
        },
    }


def apply_session_limits(
    cur,
    *,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
) -> None:
    """Apply per-transaction statement_timeout to a psycopg cursor.

    Must be called inside an explicit transaction (BEGIN ... COMMIT).
    """
    cur.execute("SET LOCAL statement_timeout = %s", (int(statement_timeout_ms),))


def jsonify(value: Any) -> Any:
    """Convert a psycopg/db value (recursively) to a JSON-safe Python type.

    UUID → str, datetime/date → ISO 8601, timedelta → float seconds,
    Decimal → float, bytes → utf-8 (with replacement), lists/tuples/
    sets → list, dicts/Row → dict-of-jsonify.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).decode("utf-8", errors="replace")
        except Exception:
            return "<bytes>"
    if isinstance(value, dict):
        return {k: jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonify(v) for v in value]
    # psycopg Row objects are dict-like; handled below.
    try:
        if hasattr(value, "_asdict"):
            return jsonify(value._asdict())
        if hasattr(value, "items"):
            return {k: jsonify(v) for k, v in value.items()}
        if hasattr(value, "__iter__"):
            return [jsonify(v) for v in value]
    except Exception:
        pass
    return str(value)


def row_limit_cap(rows: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """Trim a result list to ``limit`` rows. Pass-through if already short."""
    if limit <= 0 or len(rows) <= limit:
        return rows
    return rows[:limit]
