# data-layer-adapters/Dockerfile
#
# Builds the framework-adapter collection for the data-layer stack.
# One image, multiple roles (dispatched by docker-entrypoint.sh via ADAPTER_ROLE):
#   - ADAPTER_ROLE=hook         run redis_publish_hook.py as a long-lived sidecar
#   - ADAPTER_ROLE=mcp          run mcp/server.py on stdio (per-MCP-protocol)
#   - ADAPTER_ROLE=smoke        run tests/dual_write_smoke.sh once and exit
#   - ADAPTER_ROLE=agent-zero   one-shot: install agent-zero deps + plugin,
#                               apply default_agent seed, exit 0
#
# Build: docker build -t data-layer-adapters data-layer-adapters/

FROM python:3.12-slim

LABEL org.opencontainers.image.title="data-layer-adapters"
LABEL org.opencontainers.image.description="Framework adapters + dual-write + MCP server + publish hook + agent-zero bootstrap"
LABEL org.opencontainers.image.source="data-layer/data-layer-adapters"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash libpq5 curl ca-certificates postgresql-client redis-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Source.
COPY lib/      /app/lib/
COPY mcp/      /app/mcp/
COPY tests/    /app/tests/
# agent-zero adapter (bootstrap + plugin + seeds) — used by ADAPTER_ROLE=agent-zero
COPY agent-zero/   /app/agent-zero/

COPY docker-entrypoint.sh /usr/local/bin/data-layer-entrypoint
RUN chmod 0755 /usr/local/bin/data-layer-entrypoint

HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD redis-cli -h "${DATA_LAYER_REDIS_HOST:-redis}" -p "${DATA_LAYER_REDIS_PORT:-6379}" PING | grep -q PONG || exit 1

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

ENTRYPOINT ["/usr/local/bin/data-layer-entrypoint"]
CMD ["hook"]
