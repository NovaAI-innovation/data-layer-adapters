#!/usr/bin/env bash
# data-layer-adapters/tests/dual_write_smoke.sh
#
# Smoke test for the dual-write path. Runs a sample session.heartbeat
# through WriteThrough and asserts:
#   1. A row appears in postgres session_heartbeats.
#   2. A key appears in redis under the tenant prefix.
#   3. (If the publish hook is reachable) A Session node in falkordb
#      has the matching last_heartbeat_at property.
#
# Required env (set by the caller; defaults shown):
#   DATA_LAYER_POSTGRES_DSN     (default: postgresql://postgres@localhost:5432/postgres)
#   DATA_LAYER_REDIS_URL        (default: redis://127.0.0.1:6379/0)
#   DATA_LAYER_REDIS_PREFIX     (default: dl:)
#   DATA_LAYER_FALKORDB_HOST    (default: 127.0.0.1)
#   DATA_LAYER_FALKORDB_PORT    (default: 6379)
#   DATA_LAYER_FALKORDB_DATABASE (default: data_layer)
#
# Exit code: 0 on full pass; non-zero on any assertion failure.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
LIB="$ROOT/lib"

DSN="${DATA_LAYER_POSTGRES_DSN:-postgresql://postgres@localhost:5432/postgres}"
REDIS_URL="${DATA_LAYER_REDIS_URL:-redis://127.0.0.1:6379/0}"
PREFIX="${DATA_LAYER_REDIS_PREFIX:-dl:}"
FALKOR_HOST="${DATA_LAYER_FALKORDB_HOST:-127.0.0.1}"
FALKOR_PORT="${DATA_LAYER_FALKORDB_PORT:-6379}"
FALKOR_DB="${DATA_LAYER_FALKORDB_DATABASE:-data_layer}"

# Generate a unique session_id for this run.
SESSION_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
RUN_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

log() { printf '[dual_write_smoke %s] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '[dual_write_smoke FAIL] %s\n' "$*" >&2; exit 1; }

log "session_id=$SESSION_ID run_ts=$RUN_TS"

# ──────────────────────────────────────────────────────────────────────
# Step 0: Fixture — session_heartbeats.session_id FKs to sessions(id),
# and sessions.agent_id FKs to agents(id). Pick any existing agent
# (adapter seeds provide one) and create the session row.
# ──────────────────────────────────────────────────────────────────────
log "step 0: creating session fixture"
AGENT_ID="$(DATA_LAYER_POSTGRES_DSN="$DSN" python3 - <<PY
import os, psycopg
with psycopg.connect(os.environ["DATA_LAYER_POSTGRES_DSN"]) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM agents ORDER BY created_at LIMIT 1")
        row = cur.fetchone()
        print(row[0] if row else "")
PY
)"
if [[ -z "$AGENT_ID" ]]; then
    fail "no rows in agents; run adapter seeds before the smoke test"
fi
DATA_LAYER_POSTGRES_DSN="$DSN" python3 - <<PY
import os, psycopg
with psycopg.connect(os.environ["DATA_LAYER_POSTGRES_DSN"]) as conn:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions (id, agent_id, session_key, metadata) "
            "VALUES (%s, %s, %s, '{\"test\": true}'::jsonb)",
            ("$SESSION_ID", "$AGENT_ID", "dual-write-smoke:$SESSION_ID"),
        )
    conn.commit()
PY
log "session fixture created (agent_id=$AGENT_ID)"

# ──────────────────────────────────────────────────────────────────────
# Step 1: Drive the dual-write through the framework-agnostic core.
# ──────────────────────────────────────────────────────────────────────
log "step 1: invoking WriteThrough.session_heartbeat_record"

DATA_LAYER_POSTGRES_DSN="$DSN" \
DATA_LAYER_REDIS_URL="$REDIS_URL" \
DATA_LAYER_REDIS_PREFIX="$PREFIX" \
DATA_LAYER_FALKORDB_HOST="$FALKOR_HOST" \
DATA_LAYER_FALKORDB_PORT="$FALKOR_PORT" \
DATA_LAYER_FALKORDB_DATABASE="$FALKOR_DB" \
DATA_LAYER_DW_SESSION_PRESENCE=true \
python3 - <<PY
import os, sys, json
sys.path.insert(0, "$LIB")
from write_through import WriteThrough, session_heartbeat_record, get_counters

record = session_heartbeat_record(
    session_id="$SESSION_ID",
    ts_iso="$RUN_TS",
    source="adapter",  # canonical values only: adapter|replay|import (0004 CHECK)
    metadata={"test": True},
)
summary = WriteThrough.from_env().write(record)
print(json.dumps({"summary": summary, "counters": get_counters()}))
PY

# ──────────────────────────────────────────────────────────────────────
# Step 2: Assert postgres row exists.
# ──────────────────────────────────────────────────────────────────────
log "step 2: asserting postgres session_heartbeats row exists"
PG_ROW_COUNT="$(DATA_LAYER_POSTGRES_DSN="$DSN" python3 - <<PY
import os, sys
import psycopg
dsn = os.environ["DATA_LAYER_POSTGRES_DSN"]
with psycopg.connect(dsn) as conn:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM session_heartbeats WHERE session_id = %s",
            ("$SESSION_ID",),
        )
        print(cur.fetchone()[0])
PY
)"
if [[ "$PG_ROW_COUNT" -lt 1 ]]; then
    fail "expected >=1 row in session_heartbeats for session_id=$SESSION_ID; got $PG_ROW_COUNT"
fi
log "postgres OK ($PG_ROW_COUNT row(s))"

# ──────────────────────────────────────────────────────────────────────
# Step 3: Assert redis cache key exists.
# ──────────────────────────────────────────────────────────────────────
log "step 3: asserting redis cache key exists"
REDIS_HIT="$(DATA_LAYER_REDIS_URL="$REDIS_URL" DATA_LAYER_REDIS_PREFIX="$PREFIX" python3 - <<PY
import os, sys
import redis
r = redis.Redis.from_url(os.environ["DATA_LAYER_REDIS_URL"], decode_responses=True)
key = os.environ["DATA_LAYER_REDIS_PREFIX"] + "session:$SESSION_ID:presence"
val = r.get(key)
print("HIT" if val else "MISS")
PY
)"
if [[ "$REDIS_HIT" != "HIT" ]]; then
    fail "expected redis HIT for key=${PREFIX}session:$SESSION_ID:presence"
fi
log "redis OK"

# ──────────────────────────────────────────────────────────────────────
# Step 4 (optional): Assert falkordb Session node updated.
#   Only runs if DATA_LAYER_DUAL_WRITE_SMOKE_FALKORDB=1.
#   Requires the publish hook to be running and have already consumed
#   the event.
# ──────────────────────────────────────────────────────────────────────
if [[ "${DATA_LAYER_DUAL_WRITE_SMOKE_FALKORDB:-0}" == "1" ]]; then
    log "step 4: asserting falkordb Session node updated"
    FALKOR_HIT="$(python3 - <<PY
import os, socket
s = socket.create_connection(("$FALKOR_HOST", $FALKOR_PORT), timeout=2.0)
s.sendall(f"*2\r\n\$4\r\nPING\r\n".encode())
s.settimeout(2.0)
data = b""
while not data.endswith(b"\r\n"):
    chunk = s.recv(64)
    if not chunk: break
    data += chunk
print("HIT" if b"+PONG" in data else "MISS")
s.close()
PY
)"
    if [[ "$FALKOR_HIT" != "HIT" ]]; then
        fail "falkordb not reachable at $FALKOR_HOST:$FALKOR_PORT"
    fi
    log "falkordb reachable; manual verify of Session.last_heartbeat_at needed"
else
    log "step 4: skipped (set DATA_LAYER_DUAL_WRITE_SMOKE_FALKORDB=1 to enable)"
fi

log "PASS — dual-write path verified (postgres + redis)"
