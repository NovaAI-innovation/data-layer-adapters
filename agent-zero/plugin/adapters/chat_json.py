"""ChatJsonAdapter — ingest Agent Zero chat directories into PostgresAdapter.

Reads `usr/chats/<chat_id>/chat.json` (the source of truth) and reconstructs:
- One session per chat (session_key = chat_id)
- One `messages` row per entry in `agents[0].history.topics[].messages[]`
- One `tool_executions` row per tool call (parent for `parallel`, children for
  each inner job), with status/result/duration parsed from the following
  tool-result message.

The `messages/<N>.txt` files are NOT the source — they are partial WebUI
snapshots (some message types, some skill reattachments). We keep them on
disk for the WebUI but never rely on them for ingest.

Idempotency: every write uses ON CONFLICT (via PostgresAdapter's own
upsert paths) keyed by external_ref = {chat_id, sequence}. Re-running the
ingenest is safe.

Usage:
    from usr.plugins.data_management.adapters.chat_json import ChatJsonAdapter
    a = ChatJsonAdapter()
    a.ingest_chat("/a0/usr/chats/A6BSeczb")
    a.ingest_all("/a0/usr/chats")
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

from usr.plugins.data_management.adapter_contract import PersistenceAdapter
from usr.plugins.data_management.adapters import PostgresAdapter


# Default project/framework/agent identifiers for the sbzm instance.
DEFAULT_PROJECT_KEY = "default"
DEFAULT_PROJECT_NAME = "Default Project"
DEFAULT_FRAMEWORK_KIND = "agent_zero"
DEFAULT_FRAMEWORK_NAME = "Agent Zero"
DEFAULT_FRAMEWORK_VERSION = "2.11"
DEFAULT_AGENT_LOCAL_ID = "agent-zero-sbzm"
DEFAULT_AGENT_NAME = "a0"


@dataclass
class IngestStats:
    chats_total: int = 0
    chats_ingested: int = 0
    chats_skipped: int = 0
    sessions_opened: int = 0
    messages_total: int = 0
    messages_recorded: int = 0
    tool_calls_total: int = 0
    tool_calls_recorded: int = 0
    parallel_parents: int = 0
    parallel_children: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "chats_total": self.chats_total,
            "chats_ingested": self.chats_ingested,
            "chats_skipped": self.chats_skipped,
            "sessions_opened": self.sessions_opened,
            "messages_total": self.messages_total,
            "messages_recorded": self.messages_recorded,
            "tool_calls_total": self.tool_calls_total,
            "tool_calls_recorded": self.tool_calls_recorded,
            "parallel_parents": self.parallel_parents,
            "parallel_children": self.parallel_children,
            "errors": len(self.errors),
            "first_errors": self.errors[:5],
        }


class ChatJsonAdapter:
    """Wraps PostgresAdapter to ingest `usr/chats/<id>/chat.json` trees.

    The wrapper is stateless beyond the inner PostgresAdapter and the
    per-chat ingest call. Re-running on the same chat is safe.
    """

    def __init__(self, db: Optional[PostgresAdapter] = None) -> None:
        self.db = db or PostgresAdapter()
        self._ensure_default_agent()

    # ---------------------------------------------------------------- setup

    def _ensure_default_agent(self) -> str:
        """Make sure (project, framework, agent) exist; return agent_id."""
        fw = self.db.register_framework(
            DEFAULT_FRAMEWORK_KIND,
            DEFAULT_FRAMEWORK_NAME,
            version=DEFAULT_FRAMEWORK_VERSION,
        )
        proj = self.db.register_project(
            DEFAULT_PROJECT_KEY,
            DEFAULT_PROJECT_NAME,
        )
        ag = self.db.register_agent(
            project_id=proj.id,
            framework_id=fw.id,
            framework_local_id=DEFAULT_AGENT_LOCAL_ID,
            display_name=DEFAULT_AGENT_NAME,
            profile_key="agent0",
        )
        return str(ag.id)

    # ---------------------------------------------------------------- public

    def ingest_chat(self, chat_dir: str, stats: IngestStats) -> bool:
        """Ingest a single chat directory. Returns True on success."""
        chat_id = os.path.basename(os.path.normpath(chat_dir))
        chat_json_path = os.path.join(chat_dir, "chat.json")
        if not os.path.isfile(chat_json_path):
            stats.chats_skipped += 1
            stats.errors.append(f"{chat_id}: no chat.json")
            return False

        try:
            with open(chat_json_path, encoding="utf-8") as f:
                chat = json.load(f)
        except Exception as exc:
            stats.chats_skipped += 1
            stats.errors.append(f"{chat_id}: chat.json parse error: {exc}")
            return False

        agents = chat.get("agents") or []
        if not agents:
            stats.chats_skipped += 1
            stats.errors.append(f"{chat_id}: no agents[]")
            return False

        # Resolve the target agent (we only persist one sbzm agent).
        agent_obj = self.db.get_agent_by_business_key(
            framework_id=self.db.get_framework(DEFAULT_FRAMEWORK_KIND).id,
            framework_local_id=DEFAULT_AGENT_LOCAL_ID,
        )
        if agent_obj is None:
            stats.chats_skipped += 1
            stats.errors.append(f"{chat_id}: agent {DEFAULT_AGENT_LOCAL_ID} missing")
            return False
        agent_id = str(agent_obj.id)

        # Open the session keyed by chat id (one session per chat).
        session_metadata = {
            "chat_name": chat.get("name"),
            "chat_type": chat.get("type"),
            "created_at": chat.get("created_at"),
            "last_message": chat.get("last_message"),
            "agent_profile": chat.get("agent_profile"),
            "agent_profile_in_agent": agents[0].get("agent_profile"),
        }
        try:
            sess = self.db.open_session(
                agent_id=agent_id,
                session_key=chat_id,
                metadata=session_metadata,
            )
            stats.sessions_opened += 1
        except Exception as exc:
            stats.chats_skipped += 1
            stats.errors.append(f"{chat_id}: open_session failed: {exc}")
            return False

        # Walk all topics, flatten messages, sort by sequence.
        flat: list[tuple[int, dict]] = []
        for ti, topic in enumerate(_iter_topics(chat)):
            for m in (topic.get("messages") or []):
                seq = int(m.get("sequence") or 0)
                flat.append((seq, m))
        flat.sort(key=lambda x: x[0])

        # Per-session pending call tracker for pairing tool_call -> result.
        pending: dict[tuple[str, int], str] = {}

        for seq, m in flat:
            stats.messages_total += 1
            external_ref = {
                "chat_id": chat_id,
                "sequence": seq,
                "topic_index": int(m.get("__topic_index") or -1),
            }
            try:
                self._ingest_message(
                    session_id=str(sess.id),
                    agent_id=agent_id,
                    msg=m,
                    external_ref=external_ref,
                    pending=pending,
                    stats=stats,
                )
                stats.messages_recorded += 1
            except Exception as exc:
                stats.errors.append(f"{chat_id} seq={seq}: {exc}")

        # Mark session closed.
        try:
            self.db.close_session(str(sess.id), status="closed")
        except Exception:
            pass

        stats.chats_ingested += 1
        return True

    def ingest_all(self, chats_root: str) -> IngestStats:
        """Ingest every `<chat_id>/chat.json` under `chats_root`."""
        stats = IngestStats()
        if not os.path.isdir(chats_root):
            stats.errors.append(f"chats_root not a directory: {chats_root}")
            return stats

        chat_ids = sorted(
            d for d in os.listdir(chats_root)
            if os.path.isdir(os.path.join(chats_root, d))
            and not d.startswith(".")
        )
        stats.chats_total = len(chat_ids)
        for cid in chat_ids:
            self.ingest_chat(os.path.join(chats_root, cid), stats)
        return stats

    # ---------------------------------------------------------------- internal

    def _ingest_message(
        self, session_id: str, agent_id: str, msg: dict,
        external_ref: dict, pending: dict[tuple[str, int], str],
        stats: IngestStats,
    ) -> None:
        ai = bool(msg.get("ai"))
        content = msg.get("content")

        # ---- Tool result (ai=False dict with tool_name/tool_result) ----
        if (not ai) and isinstance(content, dict) and content.get("tool_name"):
            tool_name = content["tool_name"]
            raw_result = content.get("tool_result")
            file_attach = content.get("file")

            # Parse parallel results into per-child completions.
            if tool_name == "parallel":
                self._finish_parallel(
                    session_id=session_id,
                    agent_id=agent_id,
                    tool_result_text=raw_result,
                    pending=pending,
                    parallel_call_seq=external_ref["sequence"] - 1,
                )
                # Also record the parallel tool-result message itself.
                self._record(
                    session_id=session_id, agent_id=agent_id,
                    direction="out", role="tool",
                    content=_jsonify_tool_result(raw_result, file_attach),
                    content_type="tool_result",
                    external_ref={**external_ref, "kind": "parallel_result"},
                )
                return

            # Single tool result: finish the matching pending execution.
            parsed_result, error_text, status = _parse_tool_result(raw_result)
            key = (tool_name, external_ref["sequence"] - 1)
            exec_id = pending.pop(key, None)
            if exec_id:
                self.db.finish_tool_execution(
                    execution_id=exec_id,
                    result=parsed_result,
                    error=error_text,
                    status=status,
                )
                stats.tool_calls_recorded += 1
            # Record the tool result message itself.
            self._record(
                session_id=session_id, agent_id=agent_id,
                direction="out", role="tool",
                content=_jsonify_tool_result(raw_result, file_attach),
                content_type="tool_result",
                external_ref={**external_ref, "kind": "tool_result"},
            )
            return

        # ---- Assistant tool call (ai=True str JSON with tool_name/tool_args)
        if ai and isinstance(content, str):
            parsed = _try_parse_json(content)
            if isinstance(parsed, dict) and parsed.get("tool_name"):
                self._ingest_assistant_tool_call(
                    session_id=session_id, agent_id=agent_id,
                    msg=msg, parsed=parsed, external_ref=external_ref,
                    pending=pending, stats=stats,
                )
                return
            # Plain assistant text / final response.
            self._record(
                session_id=session_id, agent_id=agent_id,
                direction="out", role="assistant",
                content=content, content_type="text",
                external_ref={**external_ref, "kind": "assistant_text"},
            )
            return

        # ---- Assistant legacy: dict content (rare, treat as text) ----
        if ai and isinstance(content, dict):
            self._record(
                session_id=session_id, agent_id=agent_id,
                direction="out", role="assistant",
                content=json.dumps(content, default=str),
                content_type="json",
                external_ref={**external_ref, "kind": "assistant_dict"},
            )
            return

        # ---- User prompt (ai=False dict with user_message) ----
        if (not ai) and isinstance(content, dict) and content.get("user_message") is not None:
            um = content.get("user_message") or ""
            self._record(
                session_id=session_id, agent_id=agent_id,
                direction="in", role="user",
                content=um, content_type="text",
                external_ref={**external_ref, "kind": "user_prompt"},
            )
            return

        # ---- System / skill reattachment (ai=False dict with skill_instructions)
        if (not ai) and isinstance(content, dict) and "skill_instructions" in content:
            self._record(
                session_id=session_id, agent_id=agent_id,
                direction="in", role="system",
                content=json.dumps(content, default=str),
                content_type="skill_reattachment",
                external_ref={**external_ref, "kind": "skill_reattachment"},
            )
            return

        # ---- Protocol/extras continuation marker (ai=False dict with only extras) ----
        if (not ai) and isinstance(content, dict) and ("EXTRAS" in content or "PROTOCOL" in content):
            self._record(
                session_id=session_id, agent_id=agent_id,
                direction="in", role="user",
                content=json.dumps(content, default=str),
                content_type="protocol_extras",
                external_ref={**external_ref, "kind": "protocol_extras"},
            )
            return

        # ---- Unknown ai=False dict (record raw) ----
        if (not ai) and isinstance(content, dict):
            self._record(
                session_id=session_id, agent_id=agent_id,
                direction="in", role="user",
                content=json.dumps(content, default=str),
                content_type="json",
                external_ref={**external_ref, "kind": "unknown_dict"},
            )
            return

        # ---- Plain string user message (rare) ----
        if (not ai) and isinstance(content, str):
            self._record(
                session_id=session_id, agent_id=agent_id,
                direction="in", role="user",
                content=content, content_type="text",
                external_ref={**external_ref, "kind": "user_text"},
            )
            return

        # Fallback: stringify.
        self._record(
            session_id=session_id, agent_id=agent_id,
            direction="in", role="user",
            content=str(content), content_type="text",
            external_ref={**external_ref, "kind": "fallback"},
        )

    def _ingest_assistant_tool_call(
        self, session_id: str, agent_id: str, msg: dict,
        parsed: dict, external_ref: dict,
        pending: dict[tuple[str, int], str], stats: IngestStats,
    ) -> None:
        tool_name = parsed.get("tool_name") or ""
        tool_args = parsed.get("tool_args") or {}

        # Record the assistant tool-call message itself.
        assistant_msg = self._record(
            session_id=session_id, agent_id=agent_id,
            direction="out", role="assistant",
            content=json.dumps(parsed, default=str),
            content_type="tool_call",
            external_ref={**external_ref, "kind": "assistant_tool_call"},
        )
        message_id = str(assistant_msg.id)

        # 'response' is the terminal user-facing action — no tool_result
        # row will ever arrive. Skip creating an execution to avoid an
        # orphan 'pending' row forever.
        if tool_name == "response":
            return

        if tool_name == "parallel":
            # One parent parallel execution + N children, one per inner call.
            inner_calls = tool_args.get("tool_calls") or []
            parent = self.db.start_tool_execution(
                agent_id=agent_id,
                tool_name="parallel",
                arguments={
                    "wait": tool_args.get("wait"),
                    "count": len(inner_calls),
                },
                session_id=session_id,
                message_id=message_id,
            )
            pending[("parallel", external_ref["sequence"])] = str(parent.id)
            stats.parallel_parents += 1
            stats.tool_calls_total += 1 + len(inner_calls)
            child_ids: list[str] = []
            for inner in inner_calls:
                inner_name = (inner or {}).get("tool_name") or ""
                inner_args = (inner or {}).get("tool_args") or {}
                child = self.db.start_tool_execution(
                    agent_id=agent_id,
                    tool_name=inner_name,
                    arguments=inner_args,
                    session_id=session_id,
                    message_id=message_id,
                    parent_execution_id=str(parent.id),
                )
                child_ids.append(str(child.id))
                stats.parallel_children += 1
                # Track children so _finish_parallel can resolve them.
                pending[(inner_name, external_ref["sequence"])] = str(child.id)
            return

        # Single tool call.
        stats.tool_calls_total += 1
        exec_row = self.db.start_tool_execution(
            agent_id=agent_id,
            tool_name=tool_name,
            arguments=tool_args,
            session_id=session_id,
            message_id=message_id,
        )
        pending[(tool_name, external_ref["sequence"])] = str(exec_row.id)

    def _finish_parallel(
        self, session_id: str, agent_id: str,
        tool_result_text: Any,
        pending: dict[tuple[str, int], str],
        parallel_call_seq: int,
    ) -> None:
        """Parse a parallel tool_result and finish each child execution.

        Sequence-aware pairing: pending children are keyed
        `(tool_name, parallel_call_seq)`. We only pop entries whose
        sequence equals `parallel_call_seq`, so a later same-named call
        can't accidentally be matched as a parallel child.

        Also finishes the parent parallel execution with an aggregate
        result (success/error/partial).
        """
        parsed_jobs: list[dict] = []
        if isinstance(tool_result_text, str):
            try:
                wrapped = json.loads(tool_result_text)
            except Exception:
                wrapped = {}
            if isinstance(wrapped, dict):
                parsed_jobs = wrapped.get("jobs") or []
            elif isinstance(wrapped, list):
                parsed_jobs = wrapped

        # Snapshot of pending keys belonging to this parallel group only.
        group_keys = [
            k for k in pending.keys()
            if k != ("parallel", parallel_call_seq) and k[1] == parallel_call_seq
        ]

        finished = 0
        for job in parsed_jobs:
            if not isinstance(job, dict):
                continue
            jt = job.get("tool_name") or ""
            state = job.get("state") or "unknown"
            error = job.get("error")
            result = {
                "job_id": job.get("job_id"),
                "tool_name": jt,
                "state": state,
                "context_id": job.get("context_id"),
                "duration_seconds": job.get("duration_seconds"),
            }
            # Pop the first pending child with this tool_name within
            # this parallel group only.
            matched = None
            for key in list(group_keys):
                if key[0] == jt:
                    matched = pending.pop(key)
                    group_keys.remove(key)
                    break
            if matched:
                self.db.finish_tool_execution(
                    execution_id=matched,
                    result=result,
                    error=error,
                    status="success" if state == "success" else "error",
                )
                finished += 1

        # Finish the parent parallel execution with an aggregate result.
        parent_key = ("parallel", parallel_call_seq)
        parent_id = pending.pop(parent_key, None)
        if parent_id:
            ok_count = sum(
                1 for j in parsed_jobs
                if isinstance(j, dict) and j.get("state") == "success"
            )
            total = len(parsed_jobs)
            if total == 0:
                agg_status = "success"
            elif ok_count == total:
                agg_status = "success"
            elif ok_count == 0:
                agg_status = "error"
            else:
                agg_status = "partial"
            self.db.finish_tool_execution(
                execution_id=parent_id,
                result={
                    "children": total,
                    "succeeded": ok_count,
                    "failed": total - ok_count,
                    "finished_pairs": finished,
                },
                error=None,
                status=agg_status,
        )

    def _record(
        self, session_id: str, agent_id: str,
        direction: str, role: str, content: str,
        content_type: str, external_ref: dict,
    ):
        return self.db.record_message(
            session_id=session_id,
            agent_id=agent_id,
            direction=direction,
            role=role,
            content=content,
            content_type=content_type,
            external_ref=external_ref,
        )


# ---------------------------------------------------------------- helpers


def _iter_topics(chat: dict) -> list[dict]:
    """Yield all topic-shaped segments (archived topics, bulks, current).

    Agent Zero's history uses three places for messages:
      * topics[]       — archived segments (older compacted contexts)
      * bulks[]        — rarely used; reserved for hidden context
      * current        — the active Topic holding the live conversation

    The `counter` field equals the sum of all messages across the three,
    so an ingest that only walks topics[] misses the bulk of recent
    content for chats whose live state lives in `current`.
    """
    out: list[dict] = []
    agents = chat.get("agents") or []
    if not agents:
        return out
    h = agents[0].get("history")
    if not isinstance(h, str):
        return out
    try:
        hd = json.loads(h)
    except Exception:
        return out
    if not isinstance(hd, dict):
        return out

    segments: list[dict] = []
    for t in (hd.get("topics") or []):
        if isinstance(t, dict):
            segments.append(t)
    for b in (hd.get("bulks") or []):
        if isinstance(b, dict):
            segments.append(b)
    cur = hd.get("current")
    if isinstance(cur, dict):
        segments.append(cur)

    for ti, seg in enumerate(segments):
        for m in (seg.get("messages") or []):
            if isinstance(m, dict):
                m["__topic_index"] = ti
        out.append(seg)
    return out


def _try_parse_json(s: str) -> Optional[dict]:
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s.startswith("{") and not s.startswith("["):
        return None
    try:
        v = json.loads(s)
    except Exception:
        # Some assistant messages have stray content; try stripping
        # a leading BOM / non-JSON preface.
        try:
            idx = s.find("{")
            if idx >= 0:
                v = json.loads(s[idx:])
            else:
                return None
        except Exception:
            return None
    return v if isinstance(v, dict) else None


def _parse_tool_result(raw: Any) -> tuple[Any, Optional[str], str]:
    """Return (result, error, status). Status defaults to success."""
    if raw is None:
        return None, None, "success"
    if isinstance(raw, str):
        # Try to detect explicit failure markers.
        low = raw.lower()
        if low.startswith("error") or "traceback" in low and "error" in low:
            return raw, raw[:500], "error"
        return raw, None, "success"
    if isinstance(raw, dict):
        if raw.get("error"):
            return raw, raw.get("error"), "error"
        return raw, None, "success"
    return raw, None, "success"


def _jsonify_tool_result(raw: Any, file_attach: Any) -> str:
    """Serialize a tool result into a single string for the messages table."""
    if file_attach is not None:
        return json.dumps({"result": raw, "file": file_attach}, default=str)
    if isinstance(raw, str):
        return raw
    return json.dumps(raw, default=str)
