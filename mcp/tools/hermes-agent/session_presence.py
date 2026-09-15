#!/usr/bin/env python3
"""data-layer-adapters/mcp/tools/hermes-agent/session_presence.py

Per-framework adapter for the session.presence tool (hermes-agent).

This is the hermes-agent counterpart of
mcp/tools/agent-zero/session_presence.py. The two files have
identical structure; only the FRAMEWORK constant and DEFAULT_SOURCE
differ. This is intentional: the per-framework layer must stay a
thin wrapper so the universal core in lib/write_through.py remains
the single source of truth for dual-write logic.

Per ADR docs/decisions/0001-dual-write-and-redis-publish-hook.md and
the MVP framework set in data-layer-falkordb/docs/decisions/0002-...,
these are the ONLY two per-framework variants. No langchain or
crewai variant exists.
"""
from __future__ import annotations

import os
import sys
import uuid
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADAPTERS_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", ".."))
_MCP_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_LIB_ROOT = os.path.join(_ADAPTERS_ROOT, "lib")
for p in (_LIB_ROOT, _MCP_ROOT, _ADAPTERS_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)


FRAMEWORK = "hermes-agent"
DEFAULT_SOURCE = "hermes-agent"


def record_heartbeat(
    session_id: str | uuid.UUID,
    *,
    source: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    """Record a session heartbeat using the universal dual-write path."""
    from write_through import WriteThrough, session_heartbeat_record

    src = source or DEFAULT_SOURCE
    md = dict(metadata or {})
    md.setdefault("framework", FRAMEWORK)

    record = session_heartbeat_record(
        session_id=str(session_id),
        ts_iso="",
        source=src,
        metadata=md,
    )
    record["params"].pop("ts", None)
    return WriteThrough.from_env().write(record)


def mcp_tool_descriptor() -> dict:
    return {
        "name": f"{FRAMEWORK}.session.heartbeat",
        "description": (
            f"{FRAMEWORK} wrapper for the universal session.heartbeat "
            f"tool. Tags the source as '{DEFAULT_SOURCE}' and forwards "
            f"to the dual-write path."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "metadata":   {"type": "object"},
            },
            "required": ["session_id"],
        },
    }


if __name__ == "__main__":
    import json
    print(json.dumps(mcp_tool_descriptor(), indent=2))
