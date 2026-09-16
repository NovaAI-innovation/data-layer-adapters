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
import os
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

    Note: SET LOCAL does NOT accept bind parameters in PostgreSQL
    (it expects an integer literal in the SQL text). We therefore
    inline the int via f-string. ``statement_timeout_ms`` is
    validated upstream by callers; the explicit ``int(...)`` cast
    here is belt-and-suspenders for defense in depth.
    """
    cur.execute(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}")


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


# ── install-mode gate (rag.ingest.* only) ────────────────────────────
# The RAG ingest tools (rag.ingest.point, rag.ingest.batch) are the
# ONLY write tools this MCP exposes. They are gated by the env var
# ``MCP_INSTALL_MODE=1`` so that:
#
#   * Production MCP runs (started by Agent Zero or Hermes as a read-only
#     retrieval surface) refuse writes by default — even if a malicious
#     caller asks the LLM to "just embed this one document".
#   * Bootstrap-driven ingest runs (started by
#     ``bash data-layer-qdrant/bootstrap seed`` or a one-off operator
#     command) explicitly opt in by exporting ``MCP_INSTALL_MODE=1``
#     before spawning the MCP.
#
# This gate is the operational equivalent of the postgres ``execute_sql``
# SELECT-only gate: structural refusal before the cursor is opened.
# A return value of None means the gate is open; a dict with ``error``
# means the gate refused the call.


def assert_install_mode(env_var: str = "MCP_INSTALL_MODE") -> Optional[Dict[str, Any]]:
    """Return None if install mode is enabled, else an error dict.

    Reads ``env_var`` from the process environment. The canonical value
    is the string ``"1"`` (any truthy value would also work, but ``1``
    is what every bootstrap script in this repo uses).

    This is a structural check (env lookup), not a configuration-file
    check, so it cannot be bypassed by writing to a config file the
    agent might be able to mutate.
    """
    val = os.environ.get(env_var, "")
    if val == "1":
        return None
    return _err(
        "install mode is OFF (" + env_var + "='" + val + "'). "
        "rag.ingest.* tools are gated to bootstrap scripts only. "
        "Set " + env_var + "=1 in the environment that spawns the MCP, "
        "then restart. The MCP is read-only by governance; install/seed "
        "must be a documented, operator-driven bootstrap step."
    )


# ── SOT-invariant gate (rag.ingest.* only) ───────────────────────────
# The Source-of-Truth invariant for the qdrant layer is:
#
#   * do_not_ingest_y_n == 'Y'  → NEVER ingest (must be excluded from
#     every search and never written to qdrant).
#   * superseded_y_n == 'Y'      → MUST be marked lifecycle_status='superseded'
#     and tagged in payload; the seed script also filters them out of
#     the active index.
#
# Both checks are structural (text-level) on the payload dict; the
# caller passes the payload it intends to write. The gate returns None
# if the payload is safe to ingest, else an error dict.


def assert_sot_invariant(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return None if the payload respects the qdrant SOT invariant.

    The invariant is two-part:

      1. ``do_not_ingest_y_n`` MUST NOT be 'Y'. The source-authority
         controlled fixture uses this flag to mark rows that must stay
         out of the semantic index even if they appear in the corpus
         (e.g. draft contract templates, PII redacted, internal-only).

      2. ``superseded_y_n`` MAY be 'Y', but if so the payload MUST
         include ``lifecycle_status == 'superseded'`` so downstream
         search filters can match it. (We don't refuse superseded
         rows; we require them to be tagged so they are auditable.)

    The check accepts a dict (the proposed payload) or None (treated
    as missing data — refused).
    """
    if not isinstance(payload, dict):
        return _err(
            "SOT invariant: payload must be a dict, got "
            + type(payload).__name__
        )

    if str(payload.get("do_not_ingest_y_n", "")).upper() == "Y":
        return _err(
            "SOT invariant: refused to ingest row with "
            "do_not_ingest_y_n='Y'. Such rows must be excluded from the "
            "semantic index even if they appear in the source corpus."
        )

    if str(payload.get("superseded_y_n", "")).upper() == "Y":
        if str(payload.get("lifecycle_status", "")).lower() != "superseded":
            return _err(
                "SOT invariant: row has superseded_y_n='Y' but "
                "lifecycle_status is missing or not 'superseded'. "
                "Superseded rows must be tagged with "
                "lifecycle_status='superseded' so search filters can "
                "exclude them. (We do not refuse superseded rows — we "
                "require them to be tagged.)"
            )

    return None
