# data-layer-adapters

Multi-framework adapter collection for the data-layer stack.

This project owns framework-specific code (plugin glue, prompts, hooks) and
framework-specific seed rows. It is framework-agnostic in the sense that
the postgres schema lives elsewhere (`../data-layer-postgres/`) and the
MCP server is universal (`mcp/`).

## Layout

```
data-layer-adapters/
├── agent-zero/                  # Agent Zero framework family (Jan Tomasek)
│   ├── bootstrap                # install | verify | status | reset | seed
│   ├── lib/                     # A0-specific install scripts
│   ├── plugin/                  # A0 plugin code (data_management etc.)
│   ├── prompts/                 # A0-specific prompt overrides
│   └── seeds/                   # A0 DB seed rows (idempotent)
├── hermes-agent/                # Hermes framework family (Nous Research)
│   ├── bootstrap                # same shape as agent-zero
│   ├── lib/, plugin/, prompts/, seeds/
├── mcp/                         # UNIVERSAL MCP server (not framework-specific)
│   ├── server.py
│   └── tools/
├── langchain/                   # future
└── crewai/                      # future
```

## Per-adapter commands

```bash
bash agent-zero/bootstrap seed       # apply A0 seed rows
bash hermes-agent/bootstrap seed     # apply Hermes seed rows
bash mcp/server.py                  # run the universal MCP server (stdio)
```

## Universal MCP

The MCP server in `mcp/` speaks the MCP protocol (2024-11-05). It is
not coupled to any framework. Tools are addressed by name; per-framework
tool variants live under `mcp/tools/<framework>/` when needed but the
server itself is universal.
