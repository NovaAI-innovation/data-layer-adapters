#!/usr/bin/env python3
"""data-layer-adapters/lib/write_through.py — dual-write interface.

Per ADR docs/decisions/0001-dual-write-and-redis-publish-hook.md, every
promoted state category goes through this module:

  1. INSERT/UPSERT to postgres (primary).
  2. SETEX to redis under the tenant prefix (advisory TTL).
  3. PUBLISH on the per-adapter channel; the redis_publish_hook
     consumes the event and emits the matching GRAPH.QUERY against
     falkordb.

The publish hook is the only sanctioned falkordb writer for projected
state. Adapters MUST NOT call falkordb directly.

Failure containment:

- Postgres write failure → raises; caller decides what to do (the
  primary failed; nothing else is allowed to commit).
- Redis write failure → logged; postgres is still authoritative; the
  publish hook will project on the next event for the same record.
- Publish failure → logged; same as redis write failure (idempotent
  MERGEs in falkordb make replay safe).
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterable

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore

try:
    import redis
except ImportError:  # pragma: no cover
    redis = None  # type: ignore

log = logging.getLogger("data_layer.write_through")


# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────

DEFAULT_PG_DSN = "postgresql://postgres@localhost:5432/postgres"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
DEFAULT_TENANT_PREFIX = "dl:"
DEFAULT_CHANNEL_PREFIX = "dl:pubsub:"
DEFAULT_TTL_S = 300


def _env(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val is not None and val != "" else default


# Per-category flags. Phase 3 cutover is flag-gated.
_CATEGORY_FLAGS = {
    "session_presence":      "DATA_LAYER_DW_SESSION_PRESENCE",
    "tool_execution":        "DATA_LAYER_DW_TOOL_EXECUTION",
    "idempotency_key":       "DATA_LAYER_DW_IDEMPOTENCY_KEY",
    "recent_messages":       "DATA_LAYER_DW_RECENT_MESSAGES",
    "rate_limit_event":      "DATA_LAYER_DW_RATE_LIMIT_EVENT",
    "lock_audit":            "DATA_LAYER_DW_LOCK_AUDIT",
}


def category_enabled(category: str) -> bool:
    """Return True if the dual-write path for `category` is enabled.

    Default = off. Phase 3 cutover flips each flag on per category.
    """
    flag = _CATEGORY_FLAGS.get(category)
    if flag is None:
        return False
    return _env(flag, "false").lower() in ("1", "true", "yes", "on")


# ──────────────────────────────────────────────────────────────────────
# Lightweight observability (Phase 4)
# ──────────────────────────────────────────────────────────────────────

@dataclass
class _Counters:
    pg_writes: int = 0
    redis_writes: int = 0
    falkordb_projection_attempts: int = 0
    falkordb_projection_failures: int = 0
    publish_emits: int = 0
    publish_failures: int = 0
    ttl_housekeeping_lag_rows: int = 0
    last_reset_ts: float = field(default_factory=time.time)

    def snapshot(self) -> dict:
        now = time.time()
        elapsed = max(now - self.last_reset_ts, 1e-9)
        return {
            "pg_writes_per_s":             self.pg_writes / elapsed,
            "redis_writes_per_s":          self.redis_writes / elapsed,
            "falkordb_projection_per_s":   self.falkordb_projection_attempts / elapsed,
            "falkordb_projection_failure_per_s": self.falkordb_projection_failures / elapsed,
            "publish_emit_per_s":          self.publish_emits / elapsed,
            "publish_failure_per_s":       self.publish_failures / elapsed,
            "ttl_housekeeping_lag_rows":   self.ttl_housekeeping_lag_rows,
            "uptime_s":                    elapsed,
        }

    def reset(self) -> None:
        self.pg_writes = 0
        self.redis_writes = 0
        self.falkordb_projection_attempts = 0
        self.falkordb_projection_failures = 0
        self.publish_emits = 0
        self.publish_failures = 0
        self.ttl_housekeeping_lag_rows = 0
        self.last_reset_ts = time.time()


COUNTERS = _Counters()


def get_counters() -> dict:
    """Return a snapshot of the dual-write counters.

    Intended for a `/metrics`-style endpoint or a periodic log line.
    Phase 4 ships this without a Prometheus exporter; revisit when
    usage justifies one.
    """
    return COUNTERS.snapshot()


def reset_counters() -> None:
    COUNTERS.reset()


# ──────────────────────────────────────────────────────────────────────
# The WriteThrough client
# ──────────────────────────────────────────────────────────────────────

class WriteThrough:
    """Dual-write client. See module docstring.

    Typical use:

        wt = WriteThrough.from_env()
        wt.write({
            "category": "session_presence",
            "table":    "session_heartbeats",
            "row":      {"session_id": sid, "source": "adapter"},
            "cache": {"key": f"session:{sid}:presence", "value": {...}},
            "projection": "session.heartbeat",   # None to skip falkordb
            "cypher":   ["MERGE (s:Session {id: $sid}) SET s.last_heartbeat_at = $ts"],
            "params":   {"sid": sid, "ts": ts},
        })
    """

    def __init__(
        self,
        pg_dsn: str,
        redis_url: str,
        tenant_prefix: str = DEFAULT_TENANT_PREFIX,
        channel_prefix: str = DEFAULT_CHANNEL_PREFIX,
        default_ttl_s: int = DEFAULT_TTL_S,
    ):
        if psycopg is None:
            raise RuntimeError("psycopg is required for WriteThrough")
        if redis is None:
            raise RuntimeError("redis-py is required for WriteThrough")
        self.pg_dsn = pg_dsn
        self.redis_url = redis_url
        self.tenant_prefix = tenant_prefix
        self.channel_prefix = channel_prefix
        self.default_ttl_s = default_ttl_s
        self._pg = None  # lazy
        self._r = None   # lazy

    # Convenience constructor.
    @classmethod
    def from_env(cls) -> "WriteThrough":
        return cls(
            pg_dsn=_env("DATA_LAYER_POSTGRES_DSN", DEFAULT_PG_DSN),
            redis_url=_env("DATA_LAYER_REDIS_URL", DEFAULT_REDIS_URL),
            tenant_prefix=_env("DATA_LAYER_REDIS_PREFIX", DEFAULT_TENANT_PREFIX),
            channel_prefix=_env("DATA_LAYER_PUBSUB_PREFIX", DEFAULT_CHANNEL_PREFIX),
            default_ttl_s=int(_env("DATA_LAYER_REDIS_TTL", str(DEFAULT_TTL_S))),
        )

    # Lazy clients (cheap to construct, expensive to ping).
    def _conn(self):
        if self._pg is None:
            self._pg = psycopg.connect(self.pg_dsn)
        return self._pg

    def _redis(self):
        if self._r is None:
            self._r = redis.Redis.from_url(self.redis_url, decode_responses=True)
        return self._r

    @contextmanager
    def transaction(self):
        conn = self._conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    yield cur
        except Exception:
            # Connection may be in a bad state; drop it.
            try:
                conn.close()
            except Exception:
                pass
            self._pg = None
            raise

    # The main entry point.
    def write(self, record: dict) -> dict:
        """Perform the dual-write for `record`. See class docstring.

        Returns a small summary dict (for tests / observability). Does
        not raise on redis/publish failures; only postgres failures
        propagate.
        """
        category = record["category"]
        if not category_enabled(category):
            # Dual-write disabled for this category: write to postgres
            # only (postgres is always primary). Skip redis + publish.
            self._write_pg(record)
            return {"category": category, "path": "postgres-only", "enabled": False}

        self._write_pg(record)
        COUNTERS.pg_writes += 1

        cache = record.get("cache")
        if cache:
            try:
                self._write_redis(cache)
                COUNTERS.redis_writes += 1
            except Exception as e:
                log.warning("redis write failed (non-fatal): %s", e)

        projection = record.get("projection")
        if projection:
            payload = {
                "projection": projection,
                "cypher":     record.get("cypher", []),
                "params":     record.get("params", {}),
                "event_id":   str(uuid.uuid4()),
                "ts":         time.time(),
            }
            try:
                self._redis().publish(self.channel_prefix + projection, json.dumps(payload))
                COUNTERS.publish_emits += 1
            except Exception as e:
                COUNTERS.publish_failures += 1
                log.warning("publish failed (non-fatal, hook will replay): %s", e)

        return {
            "category": category,
            "path":     "dual",
            "enabled":  True,
            "projection": bool(projection),
        }

    # ── Internals ─────────────────────────────────────────────────────

    def _write_pg(self, record: dict) -> None:
        table = record["table"]
        row = record["row"]
        on_conflict = record.get("on_conflict")
        update_cols = record.get("update_cols")
        if not row:
            raise ValueError("record.row is required")
        cols = list(row.keys())
        placeholders = ", ".join(["%s"] * len(cols))
        col_list = ", ".join(cols)
        sql_parts = [f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"]
        params: list[Any] = list(row.values())
        if on_conflict:
            sql_parts.append(f"ON CONFLICT ({on_conflict})")
            if update_cols:
                set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
                sql_parts.append(f"DO UPDATE SET {set_clause}")
            else:
                sql_parts.append("DO NOTHING")
        sql = " ".join(sql_parts)
        with self.transaction() as cur:
            cur.execute(sql, params)

    def _write_redis(self, cache: dict) -> None:
        key = self.tenant_prefix + cache["key"]
        value = cache.get("value")
        ttl = min(int(cache.get("ttl_s", self.default_ttl_s)), self.default_ttl_s)
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        self._redis().setex(key, ttl, value)


# ──────────────────────────────────────────────────────────────────────
# Convenience: build a write record for session_presence
# ──────────────────────────────────────────────────────────────────────

def session_heartbeat_record(
    session_id: str,
    ts_iso: str,
    source: str = "adapter",
    metadata: dict | None = None,
) -> dict:
    """Build a WriteThrough record for one session heartbeat."""
    metadata = metadata or {}
    cache_value = {
        "session_id": session_id,
        "last_heartbeat_at": ts_iso,
        "source": source,
    }
    return {
        "category": "session_presence",
        "table":    "session_heartbeats",
        "row":      {
            "session_id":  session_id,
            "source":      source,
            "metadata":    json.dumps(metadata),
        },
        "cache": {
            "key":   f"session:{session_id}:presence",
            "value": cache_value,
            "ttl_s": 30,
        },
        "projection": "session.heartbeat",
        "cypher":     [
            "MERGE (s:Session {id: $sid})",
            "SET s.last_heartbeat_at = $ts",
        ],
        "params":     {"sid": session_id, "ts": ts_iso},
    }


if __name__ == "__main__":
    # Smoke entry: `python lib/write_through.py` prints counter state.
    print(json.dumps(get_counters(), indent=2))
