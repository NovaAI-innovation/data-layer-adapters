"""Structured retrieval tools for the universal data-layer MCP.

Exposes 14 read-only tools (the MCP has zero write tools; see
safety.py for the SELECT-only enforcement on execute_sql):

  - health.check
  - projects.list           / projects.get
  - agents.list             / agents.get
  - sessions.list           / sessions.get
  - messages.list           / messages.get
  - messages.search
  - tool_executions.list    / tool_executions.get
  - history.retrieve
  - execute_sql             (strictly SELECT-only)

Framework-agnostic contract: no Agent Zero imports, no plugin
imports. Only psycopg + stdlib. The MCP speaks JSON-RPC stdio.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple
from uuid import UUID

import psycopg

from .safety import (
    DEFAULT_SELECT_ROW_LIMIT,
    DEFAULT_STATEMENT_TIMEOUT_MS,
    apply_session_limits,
    assert_select_only,
    jsonify,
    row_limit_cap,
)

# Hard caps protect against pathological filter args. ``limit`` is
# always clamped to a sane maximum per tool.
HARD_LIMIT_DEFAULT = 50
HARD_LIMIT_MAX = 1000
HARD_LIMIT_HISTORY = 200

# The 10 tables expected to exist in the live data-layer schema.
# health.check reports existence + row count for each. Names are
# constants defined in this module; never user input.
EXPECTED_TABLES: Tuple[str, ...] = (
    "projects",
    "agent_frameworks",
    "agents",
    "agent_skills",
    "agent_plugins",
    "available_tools",
    "hooks",
    "sessions",
    "messages",
    "tool_executions",
)


# ── helpers ──────────────────────────────────────────────────────────


def _err(message, code=-32000):
    return {"error": {"code": code, "message": "data-layer MCP: " + message}}


def _ok(payload):
    return {"result": payload}


def _bounded_limit(args, default, hard_max):
    try:
        n = int(args.get("limit", default))
    except (TypeError, ValueError):
        return default
    if n < 0:
        return 0
    return min(n, hard_max)


def _escape_like(s):
    # Postgres ILIKE uses % (any) and _ (single char) as wildcards.
    # We use | as the ESCAPE character (declared in every ILIKE query
    # via ESCAPE 0x7c), so we must escape |, %, and _ in user input
    # so a search for 100% does not match everything.
    return s.replace("|", "||").replace("%", "|%").replace("_", "|_")


def _parse_uuid(value, field):
    if value is None or value == "":
        return (False, _err(field + " is required"))
    try:
        UUID(str(value))
        return (True, str(value))
    except (ValueError, TypeError):
        return (False, _err(field + ": id must be a uuid, got " + repr(value)))


def _connect(dsn):
    return psycopg.connect(dsn, autocommit=False)


def _run_select(cur, sql, params):
    cur.execute(sql, params)
    if cur.description is None:
        return []
    cols = [d.name for d in cur.description]
    return [jsonify(dict(zip(cols, row))) for row in cur.fetchall()]


# ── health ───────────────────────────────────────────────────────────


def _tool_health_check(args, dsn):
    out = {"ok": False, "tables": {}}
    try:
        with _connect(dsn) as conn, conn.cursor() as cur:
            apply_session_limits(cur)
            cur.execute("SELECT 1 AS ok")
            out["ok"] = bool(cur.fetchone()[0])
            for t in EXPECTED_TABLES:
                # Table names are module constants; not user input.
                cur.execute("SELECT count(*) FROM " + t)  # noqa: S608
                out["tables"][t] = cur.fetchone()[0]
    except Exception as e:
        out["error"] = type(e).__name__ + ": " + str(e)
    return _ok(out)


# ── projects ─────────────────────────────────────────────────────────


def _tool_projects_list(args, dsn):
    status = args.get("status")
    limit = _bounded_limit(args, HARD_LIMIT_DEFAULT, HARD_LIMIT_MAX)
    where = "WHERE status = %s" if status else ""
    params = (status,) if status else ()
    sql = (
        "SELECT id, project_key, display_name, description, status, "
        "created_at, updated_at FROM projects "
        + where
        + " ORDER BY created_at DESC LIMIT %s"
    )
    with _connect(dsn) as conn, conn.cursor() as cur:
        apply_session_limits(cur)
        rows = _run_select(cur, sql, params + (limit,))
    return _ok({"rows": rows, "count": len(rows)})


def _tool_projects_get(args, dsn):
    key = args.get("project_key")
    if not key:
        return _err("projects.get requires project_key")
    sql = (
        "SELECT id, project_key, display_name, description, status, "
        "metadata, created_at, updated_at FROM projects WHERE project_key = %s"
    )
    with _connect(dsn) as conn, conn.cursor() as cur:
        apply_session_limits(cur)
        rows = _run_select(cur, sql, (key,))
    if not rows:
        return _err("no project with project_key=" + repr(key), code=-32004)
    return _ok(rows[0])


# ── agents ───────────────────────────────────────────────────────────


def _tool_agents_list(args, dsn):
    project_key = args.get("project_key")
    framework = args.get("framework")
    status = args.get("status")
    limit = _bounded_limit(args, HARD_LIMIT_DEFAULT, HARD_LIMIT_MAX)

    joins = []
    where_parts = []
    params = []
    if project_key:
        joins.append("JOIN projects p ON p.id = a.project_id")
        where_parts.append("p.project_key = %s")
        params.append(project_key)
    if framework:
        joins.append("JOIN agent_frameworks f ON f.id = a.framework_id")
        where_parts.append("f.kind = %s")
        params.append(framework)
    if status:
        where_parts.append("a.status = %s")
        params.append(status)
    join_clause = " ".join(joins)
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    select_f = "f.kind AS framework_kind, f.display_name AS framework_display_name, "
    sql = (
        "SELECT a.id, a.project_id, a.framework_id, " + select_f +
        "a.framework_local_id, a.deployment, a.display_name, a.profile_key, "
        "a.status, a.created_at, a.updated_at "
        "FROM agents a " + join_clause + " " + where +
        " ORDER BY a.created_at DESC LIMIT %s"
    )
    params.append(limit)
    with _connect(dsn) as conn, conn.cursor() as cur:
        apply_session_limits(cur)
        rows = _run_select(cur, sql, tuple(params))
    return _ok({"rows": rows, "count": len(rows)})


def _tool_agents_get(args, dsn):
    ok, payload = _parse_uuid(args.get("id"), "agents.get")
    if not ok:
        return payload
    aid = payload
    sql = (
        "SELECT a.id, a.project_id, a.framework_id, "
        "f.kind AS framework_kind, f.display_name AS framework_display_name, "
        "a.framework_local_id, a.deployment, a.display_name, a.profile_key, "
        "a.status, a.metadata, a.created_at, a.updated_at "
        "FROM agents a JOIN agent_frameworks f ON f.id = a.framework_id "
        "WHERE a.id = %s"
    )
    with _connect(dsn) as conn, conn.cursor() as cur:
        apply_session_limits(cur)
        rows = _run_select(cur, sql, (aid,))
    if not rows:
        return _err("no agent with id=" + repr(aid), code=-32004)
    return _ok(rows[0])


# ── sessions ─────────────────────────────────────────────────────────


def _tool_sessions_list(args, dsn):
    agent_id = args.get("agent_id")
    status = args.get("status")
    since = args.get("since")
    until = args.get("until")
    limit = _bounded_limit(args, HARD_LIMIT_DEFAULT, HARD_LIMIT_MAX)

    where_parts = []
    params = []
    if agent_id:
        where_parts.append("agent_id = %s")
        params.append(agent_id)
    if status:
        where_parts.append("status = %s")
        params.append(status)
    if since:
        where_parts.append("started_at >= %s")
        params.append(since)
    if until:
        where_parts.append("started_at < %s")
        params.append(until)
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    sql = (
        "SELECT id, agent_id, session_key, status, started_at, ended_at, "
        "metadata FROM sessions "
        + where
        + " ORDER BY started_at DESC LIMIT %s"
    )
    params.append(limit)
    with _connect(dsn) as conn, conn.cursor() as cur:
        apply_session_limits(cur)
        rows = _run_select(cur, sql, tuple(params))
    return _ok({"rows": rows, "count": len(rows)})


def _tool_sessions_get(args, dsn):
    ok, payload = _parse_uuid(args.get("id"), "sessions.get")
    if not ok:
        return payload
    sid = payload
    sql = (
        "SELECT id, agent_id, session_key, status, started_at, ended_at, "
        "metadata FROM sessions WHERE id = %s"
    )
    with _connect(dsn) as conn, conn.cursor() as cur:
        apply_session_limits(cur)
        rows = _run_select(cur, sql, (sid,))
    if not rows:
        return _err("no session with id=" + repr(sid), code=-32004)
    return _ok(rows[0])


# ── messages ─────────────────────────────────────────────────────────


def _tool_messages_list(args, dsn):
    session_id = args.get("session_id")
    agent_id = args.get("agent_id")
    role = args.get("role")
    since = args.get("since")
    until = args.get("until")
    limit = _bounded_limit(args, HARD_LIMIT_DEFAULT, HARD_LIMIT_MAX)

    where_parts = []
    params = []
    if session_id:
        where_parts.append("session_id = %s")
        params.append(session_id)
    if agent_id:
        where_parts.append("agent_id = %s")
        params.append(agent_id)
    if role:
        where_parts.append("role = %s")
        params.append(role)
    if since:
        where_parts.append("created_at >= %s")
        params.append(since)
    if until:
        where_parts.append("created_at < %s")
        params.append(until)
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    sql = (
        "SELECT id, session_id, agent_id, direction, role, content_type, "
        "substring(content from 1 for 4000) AS content, "
        "thread_id, parent_message_id, created_at "
        "FROM messages "
        + where
        + " ORDER BY created_at DESC LIMIT %s"
    )
    params.append(limit)
    with _connect(dsn) as conn, conn.cursor() as cur:
        apply_session_limits(cur)
        rows = _run_select(cur, sql, tuple(params))
    return _ok({"rows": rows, "count": len(rows)})


def _tool_messages_get(args, dsn):
    ok, payload = _parse_uuid(args.get("id"), "messages.get")
    if not ok:
        return payload
    mid = payload
    sql = (
        "SELECT id, session_id, agent_id, direction, peer_agent_id, "
        "role, content, content_type, thread_id, parent_message_id, "
        "external_ref, created_at FROM messages WHERE id = %s"
    )
    with _connect(dsn) as conn, conn.cursor() as cur:
        apply_session_limits(cur)
        rows = _run_select(cur, sql, (mid,))
    if not rows:
        return _err("no message with id=" + repr(mid), code=-32004)
    return _ok(rows[0])


def _tool_messages_search(args, dsn):
    q = args.get("q")
    if q is None or not str(q).strip():
        return _err("messages.search requires q")
    q = str(q)
    agent_id = args.get("agent_id")
    session_id = args.get("session_id")
    role = args.get("role")
    since = args.get("since")
    until = args.get("until")
    limit = _bounded_limit(args, HARD_LIMIT_DEFAULT, HARD_LIMIT_MAX)

    like = "%" + _escape_like(q) + "%"
    where_parts = ["content ILIKE %s ESCAPE 0x7c"]
    params = [like]
    if agent_id:
        where_parts.append("agent_id = %s")
        params.append(agent_id)
    if session_id:
        where_parts.append("session_id = %s")
        params.append(session_id)
    if role:
        where_parts.append("role = %s")
        params.append(role)
    if since:
        where_parts.append("created_at >= %s")
        params.append(since)
    if until:
        where_parts.append("created_at < %s")
        params.append(until)
    where = "WHERE " + " AND ".join(where_parts)
    sql = (
        "SELECT id, session_id, agent_id, role, "
        "substring(content from 1 for 4000) AS snippet, created_at "
        "FROM messages "
        + where
        + " ORDER BY created_at DESC LIMIT %s"
    )
    params.append(limit)
    with _connect(dsn) as conn, conn.cursor() as cur:
        apply_session_limits(cur)
        rows = _run_select(cur, sql, tuple(params))
    return _ok({"rows": rows, "count": len(rows), "q": q})


# ── tool_executions ──────────────────────────────────────────────────


def _tool_tool_executions_list(args, dsn):
    session_id = args.get("session_id")
    agent_id = args.get("agent_id")
    tool_name = args.get("tool_name")
    status = args.get("status")
    since = args.get("since")
    until = args.get("until")
    limit = _bounded_limit(args, HARD_LIMIT_DEFAULT, HARD_LIMIT_MAX)

    where_parts = []
    params = []
    if session_id:
        where_parts.append("session_id = %s")
        params.append(session_id)
    if agent_id:
        where_parts.append("agent_id = %s")
        params.append(agent_id)
    if tool_name:
        where_parts.append("tool_name = %s")
        params.append(tool_name)
    if status:
        where_parts.append("status = %s")
        params.append(status)
    if since:
        where_parts.append("started_at >= %s")
        params.append(since)
    if until:
        where_parts.append("started_at < %s")
        params.append(until)
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    sql = (
        "SELECT id, agent_id, session_id, message_id, tool_name, arguments, "
        "result, status, started_at, finished_at, duration_ms, error "
        "FROM tool_executions "
        + where
        + " ORDER BY started_at DESC LIMIT %s"
    )
    params.append(limit)
    with _connect(dsn) as conn, conn.cursor() as cur:
        apply_session_limits(cur)
        rows = _run_select(cur, sql, tuple(params))
    return _ok({"rows": rows, "count": len(rows)})


def _tool_tool_executions_get(args, dsn):
    ok, payload = _parse_uuid(args.get("id"), "tool_executions.get")
    if not ok:
        return payload
    eid = payload
    sql = (
        "SELECT id, agent_id, session_id, message_id, tool_name, arguments, "
        "result, status, started_at, finished_at, duration_ms, error, "
        "parent_execution_id, idempotency_key, attempt_number, "
        "retry_of_execution_id, external_ref "
        "FROM tool_executions WHERE id = %s"
    )
    with _connect(dsn) as conn, conn.cursor() as cur:
        apply_session_limits(cur)
        rows = _run_select(cur, sql, (eid,))
    if not rows:
        return _err("no tool_execution with id=" + repr(eid), code=-32004)
    return _ok(rows[0])


# ── history.retrieve ─────────────────────────────────────────────────


def _tool_history_retrieve(args, dsn):
    q = args.get("q")
    if q is None or not str(q).strip():
        return _err("history.retrieve requires q")
    q = str(q)
    agent_id = args.get("agent_id")
    role = args.get("role")
    since = args.get("since")
    until = args.get("until")
    limit = _bounded_limit(args, default=20, hard_max=HARD_LIMIT_HISTORY)

    like = "%" + _escape_like(q) + "%"

    msg_where = ["content ILIKE %s ESCAPE 0x7c"]
    msg_params = [like]
    if agent_id:
        msg_where.append("agent_id = %s")
        msg_params.append(agent_id)
    if role:
        msg_where.append("role = %s")
        msg_params.append(role)
    if since:
        msg_where.append("created_at >= %s")
        msg_params.append(since)
    if until:
        msg_where.append("created_at < %s")
        msg_params.append(until)
    msg_sql = (
        "SELECT id, session_id, agent_id, role, "
        "substring(content from 1 for 400) AS snippet, created_at "
        "FROM messages WHERE " + " AND ".join(msg_where) +
        " ORDER BY created_at DESC LIMIT %s"
    )
    msg_params = msg_params + [limit]

    te_or = "(" + " OR ".join([
        "tool_name ILIKE %s ESCAPE 0x7c",
        "COALESCE(error, 0x7c) ILIKE %s ESCAPE 0x7c",
        "COALESCE(arguments::text, 0x7c) ILIKE %s ESCAPE 0x7c",
    ]) + ")"
    te_where = [te_or]
    te_params = [like, like, like]
    if agent_id:
        te_where.append("agent_id = %s")
        te_params.append(agent_id)
    if since:
        te_where.append("started_at >= %s")
        te_params.append(since)
    if until:
        te_where.append("started_at < %s")
        te_params.append(until)
    te_sql = (
        "SELECT id, agent_id, session_id, tool_name, status, error, "
        "substring(COALESCE(arguments::text, 0x7c) from 1 for 400) AS snippet, "
        "started_at AS created_at "
        "FROM tool_executions WHERE " + " AND ".join(te_where) +
        " ORDER BY started_at DESC LIMIT %s"
    )
    te_params = te_params + [limit]

    msg_rows = []
    te_rows = []
    with _connect(dsn) as conn, conn.cursor() as cur:
        apply_session_limits(cur)
        msg_rows = _run_select(cur, msg_sql, tuple(msg_params))
        te_rows = _run_select(cur, te_sql, tuple(te_params))

    merged = []
    for r in msg_rows:
        merged.append({"source": "message", **r})
    for r in te_rows:
        merged.append({"source": "tool_execution", **r})
    merged.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    merged = merged[:limit]
    return _ok({"snippets": merged, "count": len(merged), "q": q})


# ── execute_sql ──────────────────────────────────────────────────────


def _tool_execute_sql(args, dsn):
    sql = args.get("sql")
    if sql is None or not str(sql).strip():
        return _err("execute_sql requires sql")
    sql = str(sql)
    blocked = assert_select_only(sql)
    if blocked is not None:
        return blocked
    limit = _bounded_limit(args, DEFAULT_SELECT_ROW_LIMIT, DEFAULT_SELECT_ROW_LIMIT)
    timeout_ms = int(args.get("statement_timeout_ms", DEFAULT_STATEMENT_TIMEOUT_MS))
    try:
        with _connect(dsn) as conn, conn.cursor() as cur:
            apply_session_limits(cur, statement_timeout_ms=timeout_ms)
            cur.execute(sql)
            cols = []
            rows = []
            if cur.description is not None:
                cols = [d.name for d in cur.description]
                rows = [jsonify(dict(zip(cols, row))) for row in cur.fetchall()]
            truncated = len(rows) > limit
            rows = row_limit_cap(rows, limit)
        return _ok({
            "columns": cols,
            "rows": rows,
            "count": len(rows),
            "truncated": truncated,
            "limit": limit,
        })
    except Exception as e:
        return _err("execute_sql failed: " + type(e).__name__ + ": " + str(e))


# ── registry ─────────────────────────────────────────────────────────


TOOL_REGISTRY = {
    "health.check": _tool_health_check,
    "projects.list": _tool_projects_list,
    "projects.get": _tool_projects_get,
    "agents.list": _tool_agents_list,
    "agents.get": _tool_agents_get,
    "sessions.list": _tool_sessions_list,
    "sessions.get": _tool_sessions_get,
    "messages.list": _tool_messages_list,
    "messages.get": _tool_messages_get,
    "messages.search": _tool_messages_search,
    "tool_executions.list": _tool_tool_executions_list,
    "tool_executions.get": _tool_tool_executions_get,
    "history.retrieve": _tool_history_retrieve,
    "execute_sql": _tool_execute_sql,
}
