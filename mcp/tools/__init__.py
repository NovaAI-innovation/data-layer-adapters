"""Per-framework and structured retrieval tools for the universal MCP.

This package hosts:

- ``safety.py`` — SELECT-only enforcement, statement timeout, row cap,
  JSON-safe row coercion.
- ``retrieval.py`` — 13 structured read tools for the 10-table schema.
- ``_registry.py`` — per-framework descriptor extension point (currently
  empty after the legacy heartbeat removal).
- ``<framework>/`` — per-framework thin wrappers (also empty after
  heartbeat removal; deletion is a follow-up ADR).
"""
