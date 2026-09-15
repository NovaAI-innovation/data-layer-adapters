# data-layer-adapters/Dockerfile
#
# Builds the framework-adapter collection for the data-layer stack.
# One image, multiple roles:
#   - ADAPTER_ROLE=hook     run redis_publish_hook.py as a long-lived sidecar
#   - ADAPTER_ROLE=mcp      run mcp/server.py on stdio (per-MCP-protocol)
#   - ADAPTER_ROLE=smoke    run tests/dual_write_smoke.sh once and exit
#
# Build: docker build -t data-layer-adapters data-layer-adapters/
#
# Run (hook example):
#   docker run -d --name az-publish-hook \
#     --network data_layer \
#     -e DATA_LAYER_REDIS_URL=redis://redis:6379/0 \
#     -e DATA_LAYER_FALKORDB_HOST=falkordb \
#     -e DATA_LAYER_FALKORDB_PORT=6379 \
#     -e DATA_LAYER_POSTGRES_DSN=postgresql://postgres:...@postgres:5432/postgres \
#     -e DATA_LAYER_DW_SESSION_PRESENCE=true \
#     data-layer-adapters

FROM python:3.12-slim

LABEL org.opencontainers.image.title="data-layer-adapters"
LABEL org.opencontainers.image.description="Framework adapters + dual-write + MCP server + publish hook"
LABEL org.opencontainers.image.source="data-layer/data-layer-adapters"

# System deps for psycopg + redis-py. Each RUN ends with a single backslash
# followed by a real newline — that is the correct Dockerfile line continuation.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash libpq5 curl ca-certificates postgresql-client redis-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first — better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Source.
COPY lib/      /app/lib/
COPY mcp/      /app/mcp/
COPY tests/    /app/tests/

# Entrypoint script — selects role via ADAPTER_ROLE env var.
COPY docker-entrypoint.sh /usr/local/bin/data-layer-entrypoint
RUN chmod 0755 /usr/local/bin/data-layer-entrypoint

# Healthcheck defaults to the publish-hook behaviour: probe redis.
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD redis-cli -h "${DATA_LAYER_REDIS_HOST:-redis}" -p "${DATA_LAYER_REDIS_PORT:-6379}" PING | grep -q PONG || exit 1

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

ENTRYPOINT ["/usr/local/bin/data-layer-entrypoint"]
CMD ["hook"]
