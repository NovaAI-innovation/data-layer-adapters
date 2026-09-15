#!/usr/bin/env python3
"""data-layer-adapters/lib/redis_publish_hook.py — falkordb projection subscriber.

Per ADR docs/decisions/0001-dual-write-and-redis-publish-hook.md, the
publish hook is the ONLY sanctioned writer to falkordb for projected
state. It subscribes to per-projection redis pub/sub channels and
emits the matching `GRAPH.QUERY` against falkordb.

Multi-statement Cypher is split on `;` and sent statement-by-statement
because `GRAPH.QUERY` rejects multi-statement payloads.

Run as a long-lived process: one instance per data-layer deployment.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
from typing import Iterable

try:
    import redis
except ImportError:  # pragma: no cover
    redis = None  # type: ignore

log = logging.getLogger("data_layer.publish_hook")


DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
DEFAULT_FALKORDB_HOST = "127.0.0.1"
DEFAULT_FALKORDB_PORT = 6379
DEFAULT_GRAPH = "data_layer"
DEFAULT_CHANNEL_PREFIX = "dl:pubsub:"
DEFAULT_PROJECTIONS = ("session.heartbeat",)


def _env(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val is not None and val != "" else default


# ──────────────────────────────────────────────────────────────────────
# Raw RESP client for falkordb (mirrors lib/bootstrap_payload.py).
#
# We do NOT depend on the falkordb Python client here because the
# publish hook runs as a long-lived sidecar and the lightweight raw
# RESP path is sufficient (one query at a time, MERGE-only).
# ──────────────────────────────────────────────────────────────────────

class _RESPError(Exception):
    pass


class FalkorRESP:
    """Minimal RESP client for falkordb.

    Implements the small subset needed by the publish hook:
      - GRAPH.QUERY  <graph>  <query>  [$param1 ...]
      - PING
      - SELECT <db>           (verify graph exists; non-destructive)
    """

    def __init__(self, host: str, port: int, timeout: float = 2.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None

    def connect(self) -> None:
        if self._sock is not None:
            return
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(self.timeout)
        self._sock = s

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _send(self, *args: object) -> None:
        assert self._sock is not None, "not connected"
        buf = [f"*{len(args)}\r\n"]
        for a in args:
            s = str(a)
            buf.append(f"${len(s)}\r\n{s}\r\n")
        self._sock.sendall("".join(buf).encode("utf-8"))

    def _read_line(self) -> bytes:
        assert self._sock is not None, "not connected"
        out = b""
        while not out.endswith(b"\r\n"):
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("falkordb closed connection")
            out += chunk
        return out[:-2]

    def _read_n(self, n: int) -> bytes:
        assert self._sock is not None, "not connected"
        out = b""
        while len(out) < n:
            chunk = self._sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError("falkordb closed connection")
            out += chunk
        return out

    def _read_reply(self) -> object:
        line = self._read_line()
        t = line[:1]
        if t == b"+":
            return line[1:].decode("utf-8", "replace")
        if t == b"-":
            raise _RESPError(line[1:].decode("utf-8", "replace"))
        if t == b":":
            return int(line[1:])
        if t == b"$":
            n = int(line[1:])
            if n == -1:
                return None
            return self._read_n(n).decode("utf-8", "replace")
        if t == b"*":
            n = int(line[1:])
            if n == -1:
                return None
            return [self._read_reply() for _ in range(n)]
        raise ValueError(f"unknown RESP type byte: {t!r}")

    def ping(self) -> str:
        self._send("PING")
        return self._read_reply()

    def graph_query(self, graph: str, query: str, params: dict | None = None) -> object:
        """Send one Cypher statement. Splits multi-statement queries."""
        statements = [s.strip() for s in query.split(";") if s.strip()]
        last_reply: object = None
        for stmt in statements:
            if params:
                self._send("GRAPH.QUERY", graph, stmt, *[json.dumps(v) for v in params.values()])
            else:
                self._send("GRAPH.QUERY", graph, stmt)
            last_reply = self._read_reply()
        return last_reply


# ──────────────────────────────────────────────────────────────────────
# The hook
# ──────────────────────────────────────────────────────────────────────

class RedisPublishHook:
    """Subscribes to per-projection redis pub/sub channels and projects
    to falkordb. Long-running; one process per data-layer deployment.

    Restart-safe: pub/sub events are not durable, so a missed event
    while the hook is down must be replayed by an out-of-band job
    (out of scope here; the ADR notes this).
    """

    def __init__(
        self,
        redis_url: str = DEFAULT_REDIS_URL,
        falkordb_host: str = DEFAULT_FALKORDB_HOST,
        falkordb_port: int = DEFAULT_FALKORDB_PORT,
        graph: str = DEFAULT_GRAPH,
        channel_prefix: str = DEFAULT_CHANNEL_PREFIX,
        projections: Iterable[str] = DEFAULT_PROJECTIONS,
    ):
        if redis is None:
            raise RuntimeError("redis-py is required for RedisPublishHook")
        self.redis_url = redis_url
        self.falkordb_host = falkordb_host
        self.falkordb_port = falkordb_port
        self.graph = graph
        self.channel_prefix = channel_prefix
        self.projections = tuple(projections)
        self._r: redis.Redis | None = None
        self._falkor = FalkorRESP(falkordb_host, falkordb_port)
        self._stop = threading.Event()

    @classmethod
    def from_env(cls) -> "RedisPublishHook":
        projections = _env(
            "DATA_LAYER_HOOK_PROJECTIONS",
            ",".join(DEFAULT_PROJECTIONS),
        ).split(",")
        projections = [p.strip() for p in projections if p.strip()]
        return cls(
            redis_url=_env("DATA_LAYER_REDIS_URL", DEFAULT_REDIS_URL),
            falkordb_host=_env("DATA_LAYER_FALKORDB_HOST", DEFAULT_FALKORDB_HOST),
            falkordb_port=int(_env("DATA_LAYER_FALKORDB_PORT", str(DEFAULT_FALKORDB_PORT))),
            graph=_env("DATA_LAYER_FALKORDB_DATABASE", DEFAULT_GRAPH),
            channel_prefix=_env("DATA_LAYER_PUBSUB_PREFIX", DEFAULT_CHANNEL_PREFIX),
            projections=projections,
        )

    def stop(self) -> None:
        self._stop.set()

    def _redis(self) -> redis.Redis:
        if self._r is None:
            self._r = redis.Redis.from_url(self.redis_url, decode_responses=True)
        return self._r

    def start(self) -> None:
        """Block forever, processing projection events."""
        log.info("redis_publish_hook starting; graph=%s projections=%s",
                 self.graph, self.projections)
        # Verify falkordb is reachable.
        try:
            self._falkor.connect()
            pong = self._falkor.ping()
            log.info("falkordb PING → %r", pong)
        except Exception as e:
            log.error("falkordb unreachable at startup: %s", e)
            raise

        channels = [self.channel_prefix + p for p in self.projections]
        pubsub = self._redis().pubsub()
        pubsub.subscribe(*channels)
        log.info("subscribed: %s", channels)

        try:
            for msg in pubsub.listen():
                if self._stop.is_set():
                    break
                if msg.get("type") != "message":
                    continue
                self._handle(msg)
        finally:
            try:
                pubsub.close()
            except Exception:
                pass
            self._falkor.close()
            log.info("redis_publish_hook stopped")

    def _handle(self, msg: dict) -> None:
        try:
            data = json.loads(msg["data"])
        except Exception as e:
            log.warning("bad event payload on %s: %s", msg.get("channel"), e)
            return

        projection = data.get("projection")
        cypher_list = data.get("cypher", [])
        params = data.get("params", {}) or {}
        event_id = data.get("event_id")
        if not projection or not cypher_list:
            log.warning("event missing projection/cypher: %s", data)
            return

        log.debug("project %s event=%s", projection, event_id)
        # Concatenate statements into one logical query; the client
        # splits on `;` for the wire protocol.
        joined = ";\n".join(cypher_list)
        try:
            self._falkor.graph_query(self.graph, joined, params)
        except Exception as e:
            log.error("falkordb projection failed for %s event=%s: %s",
                      projection, event_id, e)
            # Reconnect on next event.
            try:
                self._falkor.close()
            except Exception:
                pass


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    hook = RedisPublishHook.from_env()
    try:
        hook.start()
    except KeyboardInterrupt:
        hook.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
