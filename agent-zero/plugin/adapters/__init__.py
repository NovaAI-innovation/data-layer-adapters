"""PostgresAdapter — concrete PersistenceAdapter backed by psycopg.

Implements the abstract methods from `adapter_contract.PersistenceAdapter`
against the 10-table schema created by 0001_init.sql.

Connection: DSN from `data_management.dsn` (plugin config) with a
fallback to the `EVENT_LEDGER_DSN` env var. Same precedence the SQL
execution tool uses, for consistency.

Idempotency: every write method uses ON CONFLICT DO NOTHING or WHERE
NOT EXISTS, so re-runs are safe.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional
from uuid import UUID

try:
    import psycopg
    _HAS_PSYCOPG = True
except ImportError:
    _HAS_PSYCOPG = False

from helpers.plugins import get_plugin_config

# The contract is vendored inside the plugin so the implementation
# is self-contained and does not depend on the workdir.
from usr.plugins.data_management.adapter_contract import (
    Framework, Project, Agent, Session, Message, ToolExecution,
    AvailableTool, AgentSkill, AgentPlugin, Hook,
    PersistenceAdapter,
)


def _dsn() -> str:
    """Resolve the DSN. Prefers the EVENT_LEDGER_DSN env var so test
    runs and runtime overrides always win over a stale saved config.
    """
    env_dsn = os.environ.get("EVENT_LEDGER_DSN", "").strip()
    if env_dsn:
        return env_dsn
    cfg = get_plugin_config("data_management") or {}
    dsn = cfg.get("dsn", "").strip()
    if not dsn:
        raise RuntimeError(
            "data_management: no DSN configured. Set data_management.dsn "
            "or EVENT_LEDGER_DSN."
        )
    return dsn


@contextmanager
def _connect() -> Iterator["psycopg.Connection"]:
    if not _HAS_PSYCOPG:
        raise RuntimeError("psycopg not installed; pip install 'psycopg[binary]>=3.1'")
    with psycopg.connect(_dsn(), autocommit=False) as conn:
        yield conn


def _row_to_framework(r) -> Framework:
    return Framework(
        id=r[0], kind=r[1], display_name=r[2], version=r[3],
        metadata=r[4], created_at=r[5],
    )


def _row_to_project(r) -> Project:
    return Project(
        id=r[0], project_key=r[1], display_name=r[2], description=r[3],
        status=r[4], metadata=r[5], created_at=r[6], updated_at=r[7],
    )


def _row_to_agent(r) -> Agent:
    return Agent(
        id=r[0], project_id=r[1], framework_id=r[2],
        framework_local_id=r[3], display_name=r[4], profile_key=r[5],
        status=r[6], metadata=r[7], created_at=r[8], updated_at=r[9],
    )


def _row_to_session(r) -> Session:
    return Session(
        id=r[0], agent_id=r[1], session_key=r[2], status=r[3],
        started_at=r[4], ended_at=r[5], metadata=r[6],
    )


def _row_to_message(r) -> Message:
    return Message(
        id=r[0], session_id=r[1], agent_id=r[2], direction=r[3],
        peer_agent_id=r[4], role=r[5], content=r[6], content_type=r[7],
        thread_id=r[8], parent_message_id=r[9], external_ref=r[10],
        created_at=r[11],
    )


def _row_to_tool_execution(r) -> ToolExecution:
    return ToolExecution(
        id=r[0], agent_id=r[1], session_id=r[2], message_id=r[3],
        tool_name=r[4], arguments=r[5], result=r[6], status=r[7],
        started_at=r[8], finished_at=r[9], duration_ms=r[10],
        error=r[11], parent_execution_id=r[12], external_ref=r[13],
    )


def _row_to_available_tool(r) -> AvailableTool:
    return AvailableTool(
        id=r[0], agent_id=r[1], tool_key=r[2], category=r[3],
        version=r[4], manifest=r[5], enabled=r[6],
        granted_at=r[7], revoked_at=r[8], metadata=r[9],
    )


def _row_to_agent_skill(r) -> AgentSkill:
    return AgentSkill(
        id=r[0], agent_id=r[1], skill_key=r[2], source=r[3],
        version=r[4], manifest=r[5], enabled=r[6], created_at=r[7],
    )


def _row_to_agent_plugin(r) -> AgentPlugin:
    return AgentPlugin(
        id=r[0], agent_id=r[1], plugin_key=r[2], version=r[3],
        manifest=r[4], enabled=r[5], installed_at=r[6],
    )


def _row_to_hook(r) -> Hook:
    return Hook(
        id=r[0], agent_id=r[1], event_type=r[2], handler_key=r[3],
        priority=r[4], config=r[5], enabled=r[6],
        created_at=r[7], updated_at=r[8],
    )


class PostgresAdapter(PersistenceAdapter):
    """Concrete PersistenceAdapter implementation for Postgres.

    Every write method is idempotent. Reads return dataclasses or None.
    """

    # ---- Identity ----

    def register_project(
        self, project_key, display_name,
        description=None, metadata=None,
    ) -> Project:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO projects (project_key, display_name, description, metadata)
                VALUES (%s, %s, %s, COALESCE(%s, '{}'::jsonb))
                ON CONFLICT (project_key) DO UPDATE
                  SET display_name = EXCLUDED.display_name,
                      description  = EXCLUDED.description,
                      metadata     = EXCLUDED.metadata,
                      updated_at   = now()
                RETURNING id, project_key, display_name, description, status,
                          metadata, created_at, updated_at
                """,
                (project_key, display_name, description,
                 psycopg.types.json.Jsonb(metadata) if metadata else None),
            )
            return _row_to_project(cur.fetchone())

    def get_project(self, project_key) -> Optional[Project]:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, project_key, display_name, description, status, "
                "metadata, created_at, updated_at FROM projects WHERE project_key=%s",
                (project_key,),
            )
            r = cur.fetchone()
            return _row_to_project(r) if r else None

    def register_framework(
        self, kind, display_name, version=None, metadata=None,
    ) -> Framework:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_frameworks (kind, display_name, version, metadata)
                VALUES (%s, %s, %s, COALESCE(%s, '{}'::jsonb))
                ON CONFLICT (kind) DO UPDATE
                  SET display_name = EXCLUDED.display_name,
                      version      = EXCLUDED.version,
                      metadata     = EXCLUDED.metadata
                RETURNING id, kind, display_name, version, metadata, created_at
                """,
                (kind, display_name, version,
                 psycopg.types.json.Jsonb(metadata) if metadata else None),
            )
            return _row_to_framework(cur.fetchone())

    def get_framework(self, kind) -> Optional[Framework]:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, kind, display_name, version, metadata, created_at "
                "FROM agent_frameworks WHERE kind=%s", (kind,),
            )
            r = cur.fetchone()
            return _row_to_framework(r) if r else None

    def register_agent(
        self, project_id, framework_id, framework_local_id,
        display_name=None, profile_key=None, metadata=None,
    ) -> Agent:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agents
                  (project_id, framework_id, framework_local_id,
                   display_name, profile_key, metadata)
                VALUES (%s, %s, %s, %s, %s, COALESCE(%s, '{}'::jsonb))
                ON CONFLICT (framework_id, framework_local_id) DO UPDATE
                  SET display_name = EXCLUDED.display_name,
                      profile_key  = EXCLUDED.profile_key,
                      metadata     = EXCLUDED.metadata,
                      updated_at   = now()
                RETURNING id, project_id, framework_id, framework_local_id,
                          display_name, profile_key, status, metadata,
                          created_at, updated_at
                """,
                (project_id, framework_id, framework_local_id,
                 display_name, profile_key,
                 psycopg.types.json.Jsonb(metadata) if metadata else None),
            )
            return _row_to_agent(cur.fetchone())

    def get_agent(self, agent_id) -> Optional[Agent]:
        return self._fetch_agent(
            "SELECT id, project_id, framework_id, framework_local_id, "
            "display_name, profile_key, status, metadata, created_at, updated_at "
            "FROM agents WHERE id=%s", (agent_id,)
        )

    def get_agent_by_business_key(self, framework_id, framework_local_id) -> Optional[Agent]:
        return self._fetch_agent(
            "SELECT id, project_id, framework_id, framework_local_id, "
            "display_name, profile_key, status, metadata, created_at, updated_at "
            "FROM agents WHERE framework_id=%s AND framework_local_id=%s",
            (framework_id, framework_local_id),
        )

    def _fetch_agent(self, sql, params) -> Optional[Agent]:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            r = cur.fetchone()
            return _row_to_agent(r) if r else None

    # ---- Sessions ----

    def open_session(self, agent_id, session_key, metadata=None) -> Session:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sessions (agent_id, session_key, metadata)
                VALUES (%s, %s, COALESCE(%s, '{}'::jsonb))
                ON CONFLICT (agent_id, session_key) DO UPDATE
                  SET metadata = EXCLUDED.metadata
                RETURNING id, agent_id, session_key, status, started_at,
                          ended_at, metadata
                """,
                (agent_id, session_key,
                 psycopg.types.json.Jsonb(metadata) if metadata else None),
            )
            return _row_to_session(cur.fetchone())

    def close_session(self, session_id, status="closed") -> None:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET status=%s, ended_at=now() WHERE id=%s",
                (status, session_id),
            )
            conn.commit()

    def get_session(self, session_id) -> Optional[Session]:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, agent_id, session_key, status, started_at, "
                "ended_at, metadata FROM sessions WHERE id=%s", (session_id,)
            )
            r = cur.fetchone()
            return _row_to_session(r) if r else None

    # ---- Messages ----

    def record_message(
        self, session_id, agent_id, direction, role, content,
        content_type="text", peer_agent_id=None, thread_id=None,
        parent_message_id=None, external_ref=None,
    ) -> Message:
        with _connect() as conn, conn.cursor() as cur:
            # Idempotency: when external_ref carries (chat_id, sequence,
            # kind), refuse to insert a duplicate row. Re-running the
            # same ingest returns the existing row instead of creating
            # a new UUID.
            if external_ref and all(
                k in external_ref for k in ("chat_id", "sequence", "kind")
            ):
                cur.execute(
                    "SELECT id, session_id, agent_id, direction, peer_agent_id, "
                    "role, content, content_type, thread_id, parent_message_id, "
                    "external_ref, created_at FROM messages "
                    "WHERE session_id=%s "
                    "  AND external_ref->>'chat_id'=%s "
                    "  AND external_ref->>'sequence'=%s "
                    "  AND external_ref->>'kind'=%s "
                    "LIMIT 1",
                    (session_id,
                     str(external_ref["chat_id"]),
                     str(external_ref["sequence"]),
                     str(external_ref["kind"])),
                )
                existing = cur.fetchone()
                if existing:
                    conn.commit()
                    return _row_to_message(existing)

            cur.execute(
                """
                INSERT INTO messages (
                  session_id, agent_id, direction, peer_agent_id,
                  role, content, content_type, thread_id,
                  parent_message_id, external_ref)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        COALESCE(%s, '{}'::jsonb))
                ON CONFLICT (id) DO NOTHING
                RETURNING id, session_id, agent_id, direction, peer_agent_id,
                          role, content, content_type, thread_id,
                          parent_message_id, external_ref, created_at
                """,
                (session_id, agent_id, direction, peer_agent_id,
                 role, content, content_type, thread_id, parent_message_id,
                 psycopg.types.json.Jsonb(external_ref) if external_ref else None),
            )
            r = cur.fetchone()
            if r is None:
                # Already exists (uuid conflict); fetch it back
                cur.execute(
                    "SELECT id, session_id, agent_id, direction, peer_agent_id, "
                    "role, content, content_type, thread_id, parent_message_id, "
                    "external_ref, created_at FROM messages WHERE id=%s",
                    (self._find_message_id(cur, session_id, content, content_type),),
                )
                r = cur.fetchone()
            conn.commit()
            return _row_to_message(r)

    def _find_message_id(self, cur, session_id, content, content_type):
        cur.execute(
            "SELECT id FROM messages WHERE session_id=%s AND content=%s "
            "AND content_type=%s ORDER BY created_at DESC LIMIT 1",
            (session_id, content, content_type),
        )
        r = cur.fetchone()
        return r[0] if r else None

    def get_messages(
        self, session_id=None, agent_id=None, limit=None, since=None,
    ) -> Iterator[Message]:
        clauses = []
        params: list = []
        if session_id is not None:
            clauses.append("session_id=%s")
            params.append(session_id)
        if agent_id is not None:
            clauses.append("agent_id=%s")
            params.append(agent_id)
        if since is not None:
            clauses.append("created_at >= %s")
            params.append(since)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        lim = f"LIMIT {int(limit)}" if limit else ""
        sql = (
            f"SELECT id, session_id, agent_id, direction, peer_agent_id, "
            f"role, content, content_type, thread_id, parent_message_id, "
            f"external_ref, created_at FROM messages {where} "
            f"ORDER BY created_at ASC {lim}"
        )
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            while True:
                r = cur.fetchone()
                if r is None:
                    return
                yield _row_to_message(r)

    # ---- Tool executions ----

    def start_tool_execution(
        self, agent_id, tool_name, arguments,
        session_id=None, message_id=None, parent_execution_id=None,
        external_ref=None,
    ) -> ToolExecution:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tool_executions (
                  agent_id, session_id, message_id, tool_name, arguments,
                  status, parent_execution_id, external_ref)
                VALUES (%s, %s, %s, %s, %s, 'pending', %s,
                        COALESCE(%s, '{}'::jsonb))
                RETURNING id, agent_id, session_id, message_id, tool_name,
                          arguments, result, status, started_at, finished_at,
                          duration_ms, error, parent_execution_id, external_ref
                """,
                (agent_id, session_id, message_id, tool_name,
                 psycopg.types.json.Jsonb(arguments), parent_execution_id,
                 psycopg.types.json.Jsonb(external_ref) if external_ref else None),
            )
            return _row_to_tool_execution(cur.fetchone())

    def finish_tool_execution(
        self, execution_id, result=None, error=None, status="success",
    ) -> ToolExecution:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tool_executions SET
                  result      = COALESCE(%s, result),
                  error       = %s,
                  status      = %s,
                  finished_at = now(),
                  duration_ms = COALESCE(
                    EXTRACT(MILLISECOND FROM (now() - started_at))::int,
                    duration_ms
                  )
                WHERE id=%s
                RETURNING id, agent_id, session_id, message_id, tool_name,
                          arguments, result, status, started_at, finished_at,
                          duration_ms, error, parent_execution_id, external_ref
                """,
                (psycopg.types.json.Jsonb(result) if result else None,
                 error, status, execution_id),
            )
            r = cur.fetchone()
            conn.commit()
            return _row_to_tool_execution(r)

    def get_tool_executions(
        self, session_id=None, agent_id=None, tool_name=None, since=None,
    ) -> Iterator[ToolExecution]:
        clauses, params = [], []
        if session_id is not None:
            clauses.append("session_id=%s")
            params.append(session_id)
        if agent_id is not None:
            clauses.append("agent_id=%s")
            params.append(agent_id)
        if tool_name is not None:
            clauses.append("tool_name=%s")
            params.append(tool_name)
        if since is not None:
            clauses.append("started_at >= %s")
            params.append(since)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"SELECT id, agent_id, session_id, message_id, tool_name, "
            f"arguments, result, status, started_at, finished_at, "
            f"duration_ms, error, parent_execution_id, external_ref "
            f"FROM tool_executions {where} ORDER BY started_at ASC"
        )
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            while True:
                r = cur.fetchone()
                if r is None:
                    return
                yield _row_to_tool_execution(r)

    # ---- Capabilities ----

    def grant_tool(
        self, agent_id, tool_key, category=None, version=None, manifest=None,
    ) -> AvailableTool:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO available_tools
                  (agent_id, tool_key, category, version, manifest)
                VALUES (%s, %s, %s, %s, COALESCE(%s, '{}'::jsonb))
                ON CONFLICT (agent_id, tool_key) DO UPDATE
                  SET category = EXCLUDED.category,
                      version  = EXCLUDED.version,
                      manifest = EXCLUDED.manifest
                RETURNING id, agent_id, tool_key, category, version, manifest,
                          enabled, granted_at, revoked_at, metadata
                """,
                (agent_id, tool_key, category, version,
                 psycopg.types.json.Jsonb(manifest) if manifest else None),
            )
            return _row_to_available_tool(cur.fetchone())

    def revoke_tool(self, agent_id, tool_key) -> None:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE available_tools SET enabled=false, revoked_at=now() "
                "WHERE agent_id=%s AND tool_key=%s",
                (agent_id, tool_key),
            )
            conn.commit()

    def register_skill(
        self, agent_id, skill_key, source, version=None, manifest=None,
    ) -> AgentSkill:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_skills
                  (agent_id, skill_key, source, version, manifest)
                VALUES (%s, %s, %s, %s, COALESCE(%s, '{}'::jsonb))
                ON CONFLICT (agent_id, skill_key, source) DO UPDATE
                  SET version=EXCLUDED.version, manifest=EXCLUDED.manifest
                RETURNING id, agent_id, skill_key, source, version, manifest,
                          enabled, created_at
                """,
                (agent_id, skill_key, source, version,
                 psycopg.types.json.Jsonb(manifest) if manifest else None),
            )
            return _row_to_agent_skill(cur.fetchone())

    def install_plugin(
        self, agent_id, plugin_key, version=None, manifest=None,
    ) -> AgentPlugin:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_plugins
                  (agent_id, plugin_key, version, manifest)
                VALUES (%s, %s, %s, COALESCE(%s, '{}'::jsonb))
                ON CONFLICT (agent_id, plugin_key) DO UPDATE
                  SET version=EXCLUDED.version, manifest=EXCLUDED.manifest
                RETURNING id, agent_id, plugin_key, version, manifest,
                          enabled, installed_at
                """,
                (agent_id, plugin_key, version,
                 psycopg.types.json.Jsonb(manifest) if manifest else None),
            )
            return _row_to_agent_plugin(cur.fetchone())

    def register_hook(
        self, agent_id, event_type, handler_key, priority=100, config=None,
    ) -> Hook:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hooks
                  (agent_id, event_type, handler_key, priority, config)
                VALUES (%s, %s, %s, %s, COALESCE(%s, '{}'::jsonb))
                ON CONFLICT (agent_id, event_type, handler_key) DO UPDATE
                  SET priority=EXCLUDED.priority, config=EXCLUDED.config,
                      updated_at=now()
                RETURNING id, agent_id, event_type, handler_key, priority,
                          config, enabled, created_at, updated_at
                """,
                (agent_id, event_type, handler_key, priority,
                 psycopg.types.json.Jsonb(config) if config else None),
            )
            return _row_to_hook(cur.fetchone())

    def list_hooks(self, agent_id, event_type) -> list[Hook]:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, agent_id, event_type, handler_key, priority, "
                "config, enabled, created_at, updated_at FROM hooks "
                "WHERE agent_id=%s AND event_type=%s AND enabled=true "
                "ORDER BY priority ASC, handler_key ASC",
                (agent_id, event_type),
            )
            return [_row_to_hook(r) for r in cur.fetchall()]

    # ---- Lifecycle ----

    def close(self) -> None:
        # psycopg connections are short-lived; nothing to pool-close here.
        return None
