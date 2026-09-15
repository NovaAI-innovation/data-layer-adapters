"""Persistence adapter contract — framework-agnostic interface.

This module defines the abstract interface that decouples Agent Zero
runtime from the persistence layer. The same contract is satisfied by:

- ``PostgresAdapter``  — the only implementation today (Postgres-only).
- ``FalkorAdapter``    — future, for graph projections (nodes/edges).
- ``RedisAdapter``    — future, for hot reads / cache.

The adapter does not know about frameworks. Frameworks are registered as
rows in ``agent_frameworks`` and resolved at write time via the
``(framework_id, framework_local_id)`` business key.

Usage:

    adapter = PostgresAdapter(dsn="postgresql://...")
    fw = adapter.register_framework("agent_zero", "Agent Zero")
    agent = adapter.register_agent(project_id, fw.id, "local-root-uuid-...")
    sess = adapter.open_session(agent.id, "chat-123")
    msg = adapter.record_message(sess.id, agent.id, "in", "user", "hi")
    ex = adapter.start_tool_execution(agent.id, "code_execution_tool", {})
    adapter.finish_tool_execution(ex.id, result={"ok": True})
    adapter.close_session(sess.id)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Optional
from uuid import UUID


# --- Data classes (return types) ---


@dataclass
class Framework:
    id: UUID
    kind: str
    display_name: str
    version: Optional[str]
    metadata: dict[str, Any]
    created_at: datetime


@dataclass
class Project:
    id: UUID
    project_key: str
    display_name: str
    description: Optional[str]
    status: str
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass
class Agent:
    id: UUID
    project_id: UUID
    framework_id: UUID
    framework_local_id: str
    display_name: Optional[str]
    profile_key: Optional[str]
    status: str
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass
class Session:
    id: UUID
    agent_id: UUID
    session_key: str
    status: str
    started_at: datetime
    ended_at: Optional[datetime]
    metadata: dict[str, Any]


@dataclass
class Message:
    id: UUID
    session_id: UUID
    agent_id: UUID
    direction: str  # 'in' | 'out'
    peer_agent_id: Optional[UUID]
    role: str  # 'user' | 'assistant' | 'tool' | 'system'
    content: str
    content_type: str
    thread_id: Optional[UUID]
    parent_message_id: Optional[UUID]
    external_ref: dict[str, Any]
    created_at: datetime


@dataclass
class ToolExecution:
    id: UUID
    agent_id: UUID
    session_id: Optional[UUID]
    message_id: Optional[UUID]
    tool_name: str
    arguments: dict[str, Any]
    result: Optional[dict[str, Any]]
    status: str  # 'pending' | 'success' | 'error' | 'blocked'
    started_at: datetime
    finished_at: Optional[datetime]
    duration_ms: Optional[int]
    error: Optional[str]
    parent_execution_id: Optional[UUID]
    external_ref: dict[str, Any]


@dataclass
class AvailableTool:
    id: UUID
    agent_id: UUID
    tool_key: str
    category: Optional[str]
    version: Optional[str]
    manifest: dict[str, Any]
    enabled: bool
    granted_at: datetime
    revoked_at: Optional[datetime]
    metadata: dict[str, Any]


@dataclass
class AgentSkill:
    id: UUID
    agent_id: UUID
    skill_key: str
    source: str  # 'core' | 'plugin' | 'user'
    version: Optional[str]
    manifest: dict[str, Any]
    enabled: bool
    created_at: datetime


@dataclass
class AgentPlugin:
    id: UUID
    agent_id: UUID
    plugin_key: str
    version: Optional[str]
    manifest: dict[str, Any]
    enabled: bool
    installed_at: datetime


@dataclass
class Hook:
    id: UUID
    agent_id: UUID
    event_type: str
    handler_key: str
    priority: int
    config: dict[str, Any]
    enabled: bool
    created_at: datetime
    updated_at: datetime


# --- Adapter interface ---


class PersistenceAdapter(ABC):
    """Framework-agnostic persistence interface.

    All write methods return the persisted row (with id and timestamps).
    Read methods return dataclasses or iterators. None means "not found".
    """

    # ---- Identity ----

    @abstractmethod
    def register_project(
        self,
        project_key: str,
        display_name: str,
        description: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Project:
        """Idempotent. Returns existing row if project_key already used."""

    @abstractmethod
    def get_project(self, project_key: str) -> Optional[Project]: ...

    @abstractmethod
    def register_framework(
        self,
        kind: str,
        display_name: str,
        version: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Framework:
        """Idempotent on (kind)."""

    @abstractmethod
    def get_framework(self, kind: str) -> Optional[Framework]: ...

    @abstractmethod
    def register_agent(
        self,
        project_id: UUID,
        framework_id: UUID,
        framework_local_id: str,
        display_name: Optional[str] = None,
        profile_key: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Agent:
        """Idempotent on (framework_id, framework_local_id)."""

    @abstractmethod
    def get_agent(self, agent_id: UUID) -> Optional[Agent]: ...

    @abstractmethod
    def get_agent_by_business_key(
        self, framework_id: UUID, framework_local_id: str
    ) -> Optional[Agent]: ...

    # ---- Sessions ----

    @abstractmethod
    def open_session(
        self,
        agent_id: UUID,
        session_key: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Session:
        """Idempotent on (agent_id, session_key) — returns existing active session if any."""

    @abstractmethod
    def close_session(self, session_id: UUID, status: str = "closed") -> None: ...

    @abstractmethod
    def get_session(self, session_id: UUID) -> Optional[Session]: ...

    # ---- Messages ----

    @abstractmethod
    def record_message(
        self,
        session_id: UUID,
        agent_id: UUID,
        direction: str,  # 'in' | 'out'
        role: str,  # 'user' | 'assistant' | 'tool' | 'system'
        content: str,
        content_type: str = "text",
        peer_agent_id: Optional[UUID] = None,
        thread_id: Optional[UUID] = None,
        parent_message_id: Optional[UUID] = None,
        external_ref: Optional[dict[str, Any]] = None,
    ) -> Message: ...

    @abstractmethod
    def get_messages(
        self,
        session_id: Optional[UUID] = None,
        agent_id: Optional[UUID] = None,
        limit: Optional[int] = None,
        since: Optional[datetime] = None,
    ) -> Iterator[Message]: ...

    # ---- Tool executions ----

    @abstractmethod
    def start_tool_execution(
        self,
        agent_id: UUID,
        tool_name: str,
        arguments: dict[str, Any],
        session_id: Optional[UUID] = None,
        message_id: Optional[UUID] = None,
        parent_execution_id: Optional[UUID] = None,
        external_ref: Optional[dict[str, Any]] = None,
    ) -> ToolExecution: ...

    @abstractmethod
    def finish_tool_execution(
        self,
        execution_id: UUID,
        result: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
        status: str = "success",
    ) -> ToolExecution: ...

    @abstractmethod
    def get_tool_executions(
        self,
        session_id: Optional[UUID] = None,
        agent_id: Optional[UUID] = None,
        tool_name: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> Iterator[ToolExecution]: ...

    # ---- Capabilities (per-agent) ----

    @abstractmethod
    def grant_tool(
        self,
        agent_id: UUID,
        tool_key: str,
        category: Optional[str] = None,
        version: Optional[str] = None,
        manifest: Optional[dict[str, Any]] = None,
    ) -> AvailableTool: ...

    @abstractmethod
    def revoke_tool(self, agent_id: UUID, tool_key: str) -> None: ...

    @abstractmethod
    def register_skill(
        self,
        agent_id: UUID,
        skill_key: str,
        source: str,
        version: Optional[str] = None,
        manifest: Optional[dict[str, Any]] = None,
    ) -> AgentSkill: ...

    @abstractmethod
    def install_plugin(
        self,
        agent_id: UUID,
        plugin_key: str,
        version: Optional[str] = None,
        manifest: Optional[dict[str, Any]] = None,
    ) -> AgentPlugin: ...

    @abstractmethod
    def register_hook(
        self,
        agent_id: UUID,
        event_type: str,
        handler_key: str,
        priority: int = 100,
        config: Optional[dict[str, Any]] = None,
    ) -> Hook: ...

    @abstractmethod
    def list_hooks(
        self, agent_id: UUID, event_type: str
    ) -> list[Hook]: ...

    # ---- Lifecycle ----

    @abstractmethod
    def close(self) -> None:
        """Release any held resources (connection pool, etc.)."""
