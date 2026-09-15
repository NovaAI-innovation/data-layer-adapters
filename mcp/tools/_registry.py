"""Per-framework tool descriptors (MCP extension point).

Currently empty: the legacy ``agent-zero.session.heartbeat`` and
``hermes-agent.session.heartbeat`` descriptors have been removed
because ``session.heartbeat`` is no longer exposed via MCP (the data
layer is read-only by governance; the dual-write lib at
``lib/write_through.py`` is invoked directly by
``lib/redis_publish_hook.py``, not through this MCP).

This file is kept as an extension point so a future per-framework tool
can be registered without touching ``server.py``. To add one:

  1. Drop a file under ``mcp/tools/<framework>/<tool>.py`` with a
     ``mcp_tool_descriptor() -> dict`` function.
  2. Import it here and append its descriptor to FRAMEWORK_DESCRIPTORS.
  3. The dispatcher (``server.py``) merges FRAMEWORK_DESCRIPTORS into
     the global ``tools/list`` response at startup.

Until then, FRAMEWORK_DESCRIPTORS is empty.
"""
from __future__ import annotations

from typing import Any, Dict, List

# Empty by design. See module docstring.
FRAMEWORK_DESCRIPTORS: List[Dict[str, Any]] = []
