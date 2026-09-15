#!/usr/bin/env python3
"""data-layer-adapters/mcp/server.py — universal MCP server.

Speaks the MCP protocol. Tools are framework-agnostic; tool routing
is by tool name, not by framework. Per-framework tool variants live
under mcp/tools/<framework>/ if needed, but the server itself is
universal.

Tools shipped:
  - execute_sql         (raw SQL passthrough; kept for ops/debugging)
  - session.heartbeat   (Phase 1 of the dual-write contract; exercises
                        postgres + redis + publish-hook path. Per
                        data-layer-adapters/docs/decisions/0001.)
"""
from __future__ import annotations
import json, os, sys

DSN = os.environ.get("DATA_LAYER_POSTGRES_DSN", "postgresql://postgres@localhost:5432/postgres")

# Lazy import path for the dual-write library. sys.path manipulation is
# scoped to the main() entrypoint so `import server` for tests does
# not pollute sys.path.
_LIB_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "lib"))


def _ensure_lib_path() -> None:
    if _LIB_DIR not in sys.path:
        sys.path.insert(0, _LIB_DIR)


def handle_request(req: dict) -> dict:
    method = req.get("method", "")
    if method == "initialize":
        return {"result": {"protocolVersion": "2024-11-05",
                           "serverInfo": {"name": "data-layer", "version": "0.1.0"}}}

    if method == "tools/list":
        return {"result": {"tools": [
            {"name": "execute_sql",
             "description": "Run a SQL statement against the postgres data-layer.",
             "inputSchema": {"type": "object",
                              "properties": {"sql": {"type": "string"}},
                              "required": ["sql"]}},
            {"name": "session.heartbeat",
             "description": ("Record a session heartbeat. Writes the "
                             "postgres session_heartbeats row (or uses "
                             "record_session_heartbeat()), updates "
                             "sessions.last_heartbeat_at, writes the "
                             "redis cache key under the tenant prefix, "
                             "and publishes a session.heartbeat event "
                             "for the redis publish hook to project to "
                             "falkordb."),
             "inputSchema": {"type": "object",
                              "properties": {
                                  "session_id": {"type": "string"},
                                  "source":     {"type": "string",
                                                  "default": "adapter"},
                                  "metadata":   {"type": "object"},
                              },
                              "required": ["session_id"]}},
        ]}}

    if method == "tools/call":
        params = req.get("params", {}) or {}
        name = params.get("name")
        args = params.get("arguments", {}) or {}
        if name == "execute_sql":
            return _tool_execute_sql(args)
        if name == "session.heartbeat":
            return _tool_session_heartbeat(args)
        return {"error": {"code": -32601, "message": f"unknown tool: {name}"}}

    return {"error": {"code": -32601, "message": f"unknown method: {method}"}}


# ──────────────────────────────────────────────────────────────────────
# Tool: execute_sql  (preserved verbatim from the original server)
# ──────────────────────────────────────────────────────────────────────

def _tool_execute_sql(args: dict) -> dict:
    sql = args.get("sql", "")
    try:
        import psycopg
        with psycopg.connect(DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                if cur.description:
                    cols = [d.name for d in cur.description]
                    rows = [list(r) for r in cur.fetchall()]
                    return {"result": {"columns": cols, "rows": rows}}
                return {"result": {"affected": cur.rowcount}}
    except Exception as e:
        return {"error": {"code": -32000, "message": str(e)}}


# ──────────────────────────────────────────────────────────────────────
# Tool: session.heartbeat  (Phase 1 dual-write; see ADR docs/decisions/0001)
# ──────────────────────────────────────────────────────────────────────

def _tool_session_heartbeat(args: dict) -> dict:
    session_id = args.get("session_id")
    if not session_id:
        return {"error": {"code": -32602,
                          "message": "session_id is required"}}
    source = args.get("source", "adapter")
    metadata = args.get("metadata") or {}

    _ensure_lib_path()
    try:
        # Path A: prefer the helper SQL function if available.
        import psycopg
        with psycopg.connect(DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT record_session_heartbeat(%s, %s, %s::jsonb)",
                    (session_id, source, json.dumps(metadata)),
                )
                ts = cur.fetchone()[0]
    except Exception as e:
        # Fall back to a plain INSERT if the helper function does not
        # exist yet (e.g., migration 0004 not yet applied).
        if "function" not in str(e).lower() and "does not exist" not in str(e):
            return {"error": {"code": -32000, "message": str(e)}}
        try:
            import psycopg
            with psycopg.connect(DSN) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO session_heartbeats "
                        "(session_id, source, metadata) VALUES (%s, %s, %s::jsonb)",
                        (session_id, source, json.dumps(metadata)),
                    )
                    cur.execute(
                        "UPDATE sessions SET last_heartbeat_at = now() WHERE id = %s",
                        (session_id,),
                    )
                    ts = None
        except Exception as e2:
            return {"error": {"code": -32000, "message": str(e2)}}

    # Best-effort: cache + publish. Failures here are non-fatal
    # because the postgres primary is already committed.
    cache_status = "skipped"
    publish_status = "skipped"
    try:
        from write_through import WriteThrough, session_heartbeat_record
        record = session_heartbeat_record(
            session_id=session_id,
            ts_iso=ts.isoformat() if ts else None,
            source=source,
            metadata=metadata,
        )
        if record["cache"]["value"].get("last_heartbeat_at") is None and ts:
            record["cache"]["value"]["last_heartbeat_at"] = ts.isoformat()
        summary = WriteThrough.from_env().write(record)
        cache_status = "ok" if summary.get("path") == "dual" else "skipped"
        publish_status = ("ok" if summary.get("projection") else "skipped")
    except Exception as e:
        # Don't break the tool call if the cache/publish sidecar is down.
        cache_status = f"error: {e}"

    return {"result": {
        "session_id":     session_id,
        "last_heartbeat_at": ts.isoformat() if ts else None,
        "cache":          cache_status,
        "publish":        publish_status,
    }}


def main():
    _ensure_lib_path()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle_request(req)
            resp["jsonrpc"] = "2.0"
            if "id" in req:
                resp["id"] = req["id"]
            print(json.dumps(resp), flush=True)
        except Exception as e:
            print(json.dumps({"jsonrpc": "2.0",
                              "error": {"code": -32700, "message": str(e)}}),
                  flush=True)


if __name__ == "__main__":
    main()
