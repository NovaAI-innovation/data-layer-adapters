# data-layer-adapters — project isolation directive

This file is injected into the Agent Zero system prompt when this project
is active. It captures the workspace contract.

## Workspace

ACTIVE WORKSPACE: `/a0/usr/projects/data-layer/data-layer-adapters`

The workspace owns framework-specific code (plugin glue, prompts) and
framework-specific DB seed rows. The MCP server in `mcp/` is universal
(framework-agnostic).

## Boundary

This project does NOT own:

- Postgres schema → `../data-layer-postgres`
- Redis cache layer → `../data-layer-redis`
- FalkorDB graph layer → `../data-layer-falkordb`
- Umbrella orchestration → `..`

## Cross-project communication

Framework adapters consume the postgres schema via `$DATA_LAYER_POSTGRES_DSN`
(read from the postgres submodule's `.env.example`). The MCP server
talks to postgres the same way; framework-specific code never bypasses
the schema.
