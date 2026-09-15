#!/usr/bin/env python3
"""data-layer-adapters/mcp/server.py — universal MCP server (read-only).

Speaks the MCP protocol (2024-11-05). Exposes 14 read-only tools for
the data-layer Postgres schema. Tools are framework-agnostic.

Governance: zero write tools. Writes to the DB happen via the
governed bootstrap scripts (``bash agent-zero/bootstrap seed`` or
``bash hermes-agent/bootstrap seed``) — not via this MCP. The agent
has no autonomy over DB writes.

This server replaces the prior 2-tool version (which exposed
``execute_sql`` with unrestricted writes and ``session.heartbeat``).
Both are removed: ``session.heartbeat`` is a runtime write not
needed under the install-only-write governance; ``execute_sql`` is
strengthened to strictly SELECT-only.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

# Ensure sibling tools/ package is importable regardless of how this
# file is invoked.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from tools.retrieval import TOOL_REGISTRY  # noqa: E402
from tools._registry import FRAMEWORK_DESCRIPTORS  # noqa: E402
from tools.rag import TOOL_DESCRIPTORS_RAG, TOOL_REGISTRY_RAG  # noqa: E402

# Lazy DSN resolution: env var beats saved default. Production
# deployments set DATA_LAYER_POSTGRES_DSN explicitly; dev defaults
# to localhost for in-process testing.
DSN = os.environ.get(
    "DATA_LAYER_POSTGRES_DSN",
    "postgresql://postgres@localhost:5432/postgres",
)

# Tool descriptors for tools/list. The dispatcher merges these with
# any per-framework descriptors from tools/_registry.py.
TOOL_DESCRIPTORS: List[Dict[str, Any]] = [
    {
        "name": "health.check",
        "description": (
            "Probe DSN reachability and per-table existence + row counts. "
            "No inputs."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "projects.list",
        "description": "List projects, optionally filtered by status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["active", "archived"]},
                "limit": {"type": "integer", "minimum": 0, "maximum": 1000, "default": 50},
            },
        },
    },
    {
        "name": "projects.get",
        "description": "Get one project by its business key (project_key).",
        "inputSchema": {
            "type": "object",
            "properties": {"project_key": {"type": "string"}},
            "required": ["project_key"],
        },
    },
    {
        "name": "agents.list",
        "description": (
            "List agents with optional filters (project_key, framework kind, status)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_key": {"type": "string"},
                "framework":   {"type": "string"},
                "status":      {"type": "string", "enum": ["active", "disabled", "archived"]},
                "limit":       {"type": "integer", "minimum": 0, "maximum": 1000, "default": 50},
            },
        },
    },
    {
        "name": "agents.get",
        "description": "Get one agent by id (uuid).",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "format": "uuid"}},
            "required": ["id"],
        },
    },
    {
        "name": "sessions.list",
        "description": (
            "List sessions with optional filters (agent_id, status, since, until)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "format": "uuid"},
                "status":   {"type": "string", "enum": ["active", "closed", "crashed"]},
                "since":    {"type": "string", "format": "date-time"},
                "until":    {"type": "string", "format": "date-time"},
                "limit":    {"type": "integer", "minimum": 0, "maximum": 1000, "default": 50},
            },
        },
    },
    {
        "name": "sessions.get",
        "description": "Get one session by id (uuid).",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "format": "uuid"}},
            "required": ["id"],
        },
    },
    {
        "name": "messages.list",
        "description": (
            "List messages with optional filters (session_id, agent_id, role, "
            "since, until). Content is truncated to 4000 chars."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "format": "uuid"},
                "agent_id":   {"type": "string", "format": "uuid"},
                "role":       {"type": "string", "enum": ["user", "assistant", "tool", "system"]},
                "since":      {"type": "string", "format": "date-time"},
                "until":      {"type": "string", "format": "date-time"},
                "limit":      {"type": "integer", "minimum": 0, "maximum": 1000, "default": 50},
            },
        },
    },
    {
        "name": "messages.get",
        "description": "Get one message by id (uuid). Full content returned.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "format": "uuid"}},
            "required": ["id"],
        },
    },
    {
        "name": "messages.search",
        "description": (
            "ILIKE search over messages.content with optional filters. "
            "Returns ranked snippet rows."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "q":          {"type": "string"},
                "agent_id":   {"type": "string", "format": "uuid"},
                "session_id": {"type": "string", "format": "uuid"},
                "role":       {"type": "string", "enum": ["user", "assistant", "tool", "system"]},
                "since":      {"type": "string", "format": "date-time"},
                "until":      {"type": "string", "format": "date-time"},
                "limit":      {"type": "integer", "minimum": 0, "maximum": 1000, "default": 50},
            },
            "required": ["q"],
        },
    },
    {
        "name": "tool_executions.list",
        "description": (
            "List tool_executions with optional filters (session_id, agent_id, "
            "tool_name, status, since, until)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "format": "uuid"},
                "agent_id":   {"type": "string", "format": "uuid"},
                "tool_name":  {"type": "string"},
                "status":     {"type": "string", "enum": ["pending", "success", "error", "blocked"]},
                "since":      {"type": "string", "format": "date-time"},
                "until":      {"type": "string", "format": "date-time"},
                "limit":      {"type": "integer", "minimum": 0, "maximum": 1000, "default": 50},
            },
        },
    },
    {
        "name": "tool_executions.get",
        "description": "Get one tool_execution by id (uuid). Full payload returned.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "format": "uuid"}},
            "required": ["id"],
        },
    },
    {
        "name": "history.retrieve",
        "description": (
            "Composite retrieval: free-text q across messages + tool_executions, "
            "ranked by recency. The retrieval-MCP equivalent of a search engine "
            "over the agent's history."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "q":        {"type": "string"},
                "agent_id": {"type": "string", "format": "uuid"},
                "role":     {"type": "string", "enum": ["user", "assistant", "tool", "system"]},
                "since":    {"type": "string", "format": "date-time"},
                "until":    {"type": "string", "format": "date-time"},
                "limit":    {"type": "integer", "minimum": 0, "maximum": 200, "default": 20},
            },
            "required": ["q"],
        },
    },
    {
        "name": "execute_sql",
        "description": (
            "Run a strictly SELECT-only SQL query against the data-layer. "
            "Refuses INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/TRUNCATE/GRANT/"
            "REVOKE/MERGE/CALL/... before opening the cursor. Writes are not "
            "permitted by this MCP; use the governed bootstrap scripts "
            "(bash <adapter>/bootstrap seed) for setup. The data-layer DB is "
            "SOT managed by a governed documentation system, not by the "
            "autonomy or judgement of the agent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "statement_timeout_ms": {
                    "type": "integer", "minimum": 1000, "maximum": 120000
                },
            },
            "required": ["sql"],
        },
    },
]


def _rpc_error(code, message):
    return {"error": {"code": code, "message": message}}


def handle_request(req):
    method = req.get("method", "")
    if method == "initialize":
        return {
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {
                    "name": "data-layer",
                    "version": "0.3.0",
                    "description": (
                        "Framework-agnostic retrieval MCP for the Agent Zero "
                        "data-layer. 21 read-only tools (14 Postgres-backed + 7 "
                        "Qdrant-backed RAG). Zero write tools in normal operation; "
                        "rag.ingest.* are gated by MCP_INSTALL_MODE=1 for "
                        "bootstrap-only writes. All other writes happen via the "
                        "governed bootstrap scripts."
                    ),
                },
                "capabilities": {"tools": {"listChanged": False}},
            }
        }
    if method == "tools/list":
        merged = (
            list(TOOL_DESCRIPTORS)
            + list(TOOL_DESCRIPTORS_RAG)
            + list(FRAMEWORK_DESCRIPTORS)
        )
        return {"result": {"tools": merged}}
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if not name:
            return _rpc_error(-32602, "missing tool name")
        fn = TOOL_REGISTRY.get(name)
        if fn is None:
            fn = TOOL_REGISTRY_RAG.get(name)
        if fn is None:
            return _rpc_error(-32601, "unknown tool: " + repr(name))
        try:
            return fn(args, DSN)
        except Exception as e:
            return {
                "error": {
                    "code": -32000,
                    "message": type(e).__name__ + ": " + str(e),
                }
            }
    return _rpc_error(-32601, "unknown method: " + repr(method))


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            print(
                json.dumps(
                    {"jsonrpc": "2.0", "error": {"code": -32700, "message": str(e)}}
                ),
                flush=True,
            )
            continue
        try:
            resp = handle_request(req)
        except Exception as e:
            resp = {
                "error": {
                    "code": -32603,
                    "message": type(e).__name__ + ": " + str(e),
                }
            }
        resp["jsonrpc"] = "2.0"
        if "id" in req:
            resp["id"] = req["id"]
        print(json.dumps(resp), flush=True)


if __name__ == "__main__":
    main()
