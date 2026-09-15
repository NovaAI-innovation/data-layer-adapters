# ============================================================
# DEPRECATED 2026-09-15 — DO NOT USE
# ============================================================
# The session.heartbeat tool was removed from the universal MCP
# because session.heartbeat is a runtime write tool, not an
# install/setup/seed tool. The data layer is governed as a
# Source of Truth (SOT) managed by a documented bootstrap
# pipeline, not by the autonomy or judgement of the agent.
#
# Dual-write semantics (postgres + redis + falkordb projection)
# continue to work via lib/write_through.py, invoked directly
# by lib/redis_publish_hook.py on Postgres NOTIFY — NOT through
# this MCP. There is no replacement adapter here; heartbeat
# callers should not exist in the agent path at all.
#
# Removal of this file is scheduled as a follow-up ADR. Until
# then, the descriptor it returns is NOT registered in
# mcp/tools/_registry.py and the dispatcher ignores it.
# ============================================================

#!/usr/bin/env python3
"""data-layer-adapters/mcp/tools/agent-zero/session_presence.py

Per-framework adapter for the session.presence tool.

This is a thin wrapper around the universal session.heartbeat tool in
mcp/server.py. It exists so that the per-framework call site reads
naturally:

    from data_layer_adapters.mcp.tools.agent_zero.session_presence import \
        record_heartbeat
    record_heartbeat(session_id="...", source="agent-zero")

Both agent-zero and hermes-agent share the same framework-agnostic
core (lib/write_through.py + mcp/server.py session.heartbeat). The
per-framework file only adds framework-specific defaults (source
tag, hook integration points) — it does NOT duplicate any logic.
"""
from __future__ import annotations

import os
import sys
import uuid
from typing import Any

# Make the parent mcp/server.py and lib/write_through.py importable
# regardless of how this file is invoked.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ADAPTERS_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", ".."))
_MCP_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_LIB_ROOT = os.path.join(_ADAPTERS_ROOT, "lib")
for p in (_LIB_ROOT, _MCP_ROOT, _ADAPTERS_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)


FRAMEWORK = "agent-zero"
DEFAULT_SOURCE = "agent-zero"


def record_heartbeat(
    session_id: str | uuid.UUID,
    *,
    source: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    """Record a session heartbeat using the universal dual-write path.

    Returns the dict returned by WriteThrough.write(...). Caller can
    inspect `path` ("dual" / "postgres-only") and `projection` to
    confirm both write paths fired.
    """
    from write_through import WriteThrough, session_heartbeat_record

    src = source or DEFAULT_SOURCE
    md = dict(metadata or {})
    md.setdefault("framework", FRAMEWORK)

    record = session_heartbeat_record(
        session_id=str(session_id),
        ts_iso="",  # server-side timestamp; left empty for the adapter
        source=src,
        metadata=md,
    )
    # Clear the projection payload; the adapter will publish via the
    # standard dual-write path (no need to set ts here).
    record["params"].pop("ts", None)
    return WriteThrough.from_env().write(record)


def mcp_tool_descriptor() -> dict:
    """Return the MCP tool descriptor for this framework's wrapper.

    The MCP server merges per-framework descriptors into the global
    tool list. This file's wrapper is a convenience alias; the
    canonical tool is `session.heartbeat` in the universal server.
    """
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
    # Smoke entry: prints the descriptor.
    import json
    print(json.dumps(mcp_tool_descriptor(), indent=2))
