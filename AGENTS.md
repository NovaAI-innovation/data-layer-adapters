# AGENTS.md — data-layer-adapters

Agent contract for the multi-framework adapter collection.

## Scope and ownership

This project owns framework-specific code (plugin glue, prompts, hooks)
and framework-specific DB seed rows. It does NOT own the postgres
schema (lives in `../data-layer-postgres/`), the redis cache layer,
the falkordb graph layer, or the umbrella orchestration.

## Universal MCP

`mcp/server.py` is universal — it speaks the MCP protocol and is not
coupled to any specific framework. Tool routing is by tool name; per-
framework tool variants live under `mcp/tools/<framework>/` if needed.

## Per-framework subdirectories

- `agent-zero/` — Agent Zero framework family. Owns its own bootstrap,
  plugin/, prompts/, seeds/. Seeds run AFTER the postgres schema.
- `hermes-agent/` — Hermes framework family (Nous Research). Same
  shape as agent-zero. The two are peers; neither depends on the other.

**MVP framework set:** `agent-zero` + `hermes-agent` ONLY. No
additional framework adapters are planned. Adding a new framework
requires a new ADR (see `docs/decisions/`).

## Required workflow

Before consequential changes, read `README.md`, this file, and the
affected adapter subdirectory's README. State the intended outcome
and affected paths before implementation.
