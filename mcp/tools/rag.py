"""RAG (Retrieval-Augmented Generation) tools for the data-layer MCP.

Adds 7 Qdrant-backed tools to the universal MCP. Five are read-only
(never gated); two are gated writes (``rag.ingest.point`` and
``rag.ingest.batch``) that refuse to run unless the process
environment contains ``MCP_INSTALL_MODE=1`` AND the payload passes
the SOT invariant check.

Tools exposed (names match MCP descriptors):

  Read tools (always available):
    - rag.health
    - rag.collections.list
    - rag.collection.info
    - rag.search
    - rag.ingest.status

  Write tools (gated by MCP_INSTALL_MODE=1 + SOT invariant):
    - rag.ingest.point
    - rag.ingest.batch

Framework-agnostic contract: no Agent Zero imports, no plugin
imports. Uses the ``requests`` library (already a data-layer-adapters
requirement) to talk to the Qdrant HTTP REST API on
``DATA_LAYER_QDRANT_URL`` (default ``http://localhost:6333``).
"""
from __future__ import annotations

import csv
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid5, NAMESPACE_URL

import requests

from .safety import (
    assert_install_mode,
    assert_sot_invariant,
    jsonify,
)

# The Qdrant HTTP base URL. Production deployments set this env var
# explicitly; the default is the local Qdrant binary that data-layer-
# qdrant's bootstrap brings up on port 6333.
QDRANT_URL = os.environ.get(
    "DATA_LAYER_QDRANT_URL",
    "http://localhost:6333",
)

# Hard caps protect against pathological args. ``limit`` is always
# clamped to a sane maximum per tool.
HARD_LIMIT_RAG = 50
HARD_LIMIT_RAG_MAX = 200

# Per-call timeout for Qdrant HTTP. Keep short so a wedged Qdrant
# does not stall the MCP for minutes.
QDRANT_HTTP_TIMEOUT_S = 10.0

# The two collection names that this MCP is allowed to touch.
# Anything outside this allowlist is refused — protects against the
# agent inventing new collections that drift from the documented
# schema. See data-layer-qdrant/docs/decisions/0001-rag-sot-architecture.md.
ALLOWED_COLLECTIONS = frozenset({
    "mpg_source_authority_documents",
    "mpg_emails",
})

# Path to the JSON marker file written by data-layer-qdrant/bootstrap
# after a successful seed run. The MCP reads it for rag.ingest.status.
INGEST_STATUS_MARKER = os.environ.get(
    "DATA_LAYER_QDRANT_INGEST_MARKER",
    "/opt/qdrant/state/last_ingest.json",
)


# ── helpers ──────────────────────────────────────────────────────────


def _err(message: str, code: int = -32000) -> Dict[str, Any]:
    return {"error": {"code": code, "message": "data-layer MCP (rag): " + message}}


def _ok(payload: Any) -> Dict[str, Any]:
    return {"result": payload}


def _bounded_limit(args: Dict[str, Any], default: int, hard_max: int) -> int:
    try:
        n = int(args.get("limit", default))
    except (TypeError, ValueError):
        return default
    if n < 0:
        return 0
    return min(n, hard_max)


def _assert_collection(name: Optional[str]) -> Optional[Dict[str, Any]]:
    """Refuse any collection name outside the allowlist. Returns None on pass."""
    if not name:
        return _err("collection name is required")
    if name not in ALLOWED_COLLECTIONS:
        return _err(
            "collection '" + str(name) + "' is not in the allowlist. "
            "This MCP may only touch: " + ", ".join(sorted(ALLOWED_COLLECTIONS)) + "."
        )
    return None


def _qdrant_get(path: str) -> Dict[str, Any]:
    """GET a path on QDRANT_URL. Returns parsed JSON or an error dict."""
    url = QDRANT_URL.rstrip("/") + path
    try:
        r = requests.get(url, timeout=QDRANT_HTTP_TIMEOUT_S)
    except requests.RequestException as e:
        return _err("Qdrant unreachable at " + QDRANT_URL + ": " + type(e).__name__ + ": " + str(e))
    if r.status_code == 404:
        return _err("Qdrant returned 404 for " + path + " (collection or path missing)")
    if r.status_code >= 400:
        return _err(
            "Qdrant returned HTTP " + str(r.status_code) + " for " + path + ": " + r.text[:500]
        )
    try:
        return jsonify(r.json())
    except ValueError as e:
        return _err("Qdrant returned non-JSON for " + path + ": " + str(e))


def _qdrant_post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST JSON to QDRANT_URL. Returns parsed JSON or an error dict."""
    url = QDRANT_URL.rstrip("/") + path
    try:
        r = requests.post(url, json=body, timeout=QDRANT_HTTP_TIMEOUT_S)
    except requests.RequestException as e:
        return _err("Qdrant unreachable at " + QDRANT_URL + ": " + type(e).__name__ + ": " + str(e))
    if r.status_code >= 400:
        return _err(
            "Qdrant returned HTTP " + str(r.status_code) + " for " + path + ": " + r.text[:500]
        )
    try:
        return jsonify(r.json())
    except ValueError as e:
        return _err("Qdrant returned non-JSON for " + path + ": " + str(e))


def _qdrant_put(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """PUT JSON to QDRANT_URL. Returns parsed JSON or an error dict."""
    url = QDRANT_URL.rstrip("/") + path
    try:
        r = requests.put(url, json=body, timeout=QDRANT_HTTP_TIMEOUT_S)
    except requests.RequestException as e:
        return _err("Qdrant unreachable at " + QDRANT_URL + ": " + type(e).__name__ + ": " + str(e))
    if r.status_code >= 400:
        return _err(
            "Qdrant returned HTTP " + str(r.status_code) + " for " + path + ": " + r.text[:500]
        )
    try:
        return jsonify(r.json())
    except ValueError as e:
        return _err("Qdrant returned non-JSON for " + path + ": " + str(e))


# ── read tools ────────────────────────────────────────────────────────


def _tool_rag_health(args: Dict[str, Any], dsn: Optional[str]) -> Dict[str, Any]:
    """GET /healthz — Qdrant liveness probe.

    Qdrant's /healthz returns a plain-text body ("healthz check passed"),
    NOT JSON. So we cannot use the shared ``_qdrant_get`` helper here
    (which calls ``r.json()``). We probe the URL directly and report
    based on HTTP status.
    """
    url = QDRANT_URL.rstrip("/") + "/healthz"
    try:
        r = requests.get(url, timeout=QDRANT_HTTP_TIMEOUT_S)
    except requests.RequestException as e:
        return _ok({
            "ok": False,
            "qdrant_url": QDRANT_URL,
            "error": type(e).__name__ + ": " + str(e),
        })
    return _ok({
        "ok": (200 <= r.status_code < 300),
        "qdrant_url": QDRANT_URL,
        "status_code": r.status_code,
        "body": r.text.strip()[:500],
    })


def _tool_rag_collections_list(args: Dict[str, Any], dsn: Optional[str]) -> Dict[str, Any]:
    """GET /collections — list all collections (the MCP does not restrict the
    caller to the allowlist here; the allowlist gate is enforced on
    write-path tools only).
    """
    data = _qdrant_get("/collections")
    if isinstance(data, dict) and "error" in data:
        return data
    return _ok({
        "collections": data.get("result", {}).get("collections", []) if isinstance(data, dict) else [],
        "qdrant_url": QDRANT_URL,
    })


def _tool_rag_collection_info(args: Dict[str, Any], dsn: Optional[str]) -> Dict[str, Any]:
    """GET /collections/{name} — collection schema + point count."""
    name = args.get("collection")
    err = _assert_collection(name)
    if err:
        return err
    data = _qdrant_get("/collections/" + str(name))
    if isinstance(data, dict) and "error" in data:
        return data
    return _ok({"collection": name, "info": data})


def _tool_rag_search(args: Dict[str, Any], dsn: Optional[str]) -> Dict[str, Any]:
    """POST /collections/{name}/points/search — vector similarity search.

    Always-on filter: ``do_not_ingest_y_n != 'Y'`` AND
    ``lifecycle_status != 'superseded'``. Callers cannot override
    this filter; it's the read-side enforcement of the SOT invariant.

    Inputs:
      * collection (required, in allowlist)
      * vector (required, list of floats, length must match the
        collection's configured vector size — 768 for this stack)
      * limit (default 10, max 100)
      * filter (optional, dict — merged with the always-on SOT filter)
      * with_payload (default true)
      * with_vector (default false — vectors are big)
    """
    name = args.get("collection")
    err = _assert_collection(name)
    if err:
        return err

    vector = args.get("vector")
    if not isinstance(vector, list) or not vector:
        return _err("rag.search requires 'vector': list of floats")
    if not all(isinstance(x, (int, float)) for x in vector):
        return _err("rag.search 'vector' must be numeric")

    limit = _bounded_limit(args, default=10, hard_max=100)

    # Always-on SOT filter. MUST/MUST_NOT are combined with the
    # caller's optional filter via AND.
    sot_must = [
        {"key": "do_not_ingest_y_n", "match": {"value": "Y"}}
    ]
    # Qdrant's filter language: {must: [...], must_not: [...]}
    caller_filter = args.get("filter") or {}
    if not isinstance(caller_filter, dict):
        return _err("rag.search 'filter' must be a dict")

    merged_must = (caller_filter.get("must") or []) + sot_must
    merged_filter: Dict[str, Any] = {"must_not": sot_must, "must": caller_filter.get("must") or []}
    # Exclude superseded AND do_not_ingest rows unconditionally:
    # Use must_not with the same two clauses to be belt-and-suspenders.
    merged_filter = {
        "must": list(merged_must) + caller_filter.get("must", []),
        "must_not": list(caller_filter.get("must_not", []) or []) + [
            {"key": "do_not_ingest_y_n", "match": {"value": "Y"}},
            {"key": "lifecycle_status", "match": {"value": "superseded"}},
        ],
    }

    body = {
        "vector": vector,
        "limit": limit,
        "with_payload": bool(args.get("with_payload", True)),
        "with_vector": bool(args.get("with_vector", False)),
        "filter": merged_filter,
    }
    data = _qdrant_post("/collections/" + str(name) + "/points/search", body)
    if isinstance(data, dict) and "error" in data:
        return data
    return _ok({
        "collection": name,
        "qdrant_url": QDRANT_URL,
        "result": data.get("result") if isinstance(data, dict) else data,
    })


def _tool_rag_ingest_status(args: Dict[str, Any], dsn: Optional[str]) -> Dict[str, Any]:
    """Read the JSON marker file the bootstrap writes after a successful
    seed run. If the file does not exist, the bootstrap has never run.
    """
    p = args.get("marker_path") or INGEST_STATUS_MARKER
    if not os.path.exists(p):
        return _ok({
            "marker_path": p,
            "last_ingest": None,
            "note": "no ingest marker found at this path; bootstrap seed has not run yet",
        })
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        return _err("failed to read marker at " + p + ": " + type(e).__name__ + ": " + str(e))
    return _ok({"marker_path": p, "last_ingest": jsonify(data)})


# ── write tools (gated) ───────────────────────────────────────────────


def _tool_rag_ingest_point(args: Dict[str, Any], dsn: Optional[str]) -> Dict[str, Any]:
    """PUT /collections/{name}/points — write a single point. GATED.

    Refuses unless:
      * ``MCP_INSTALL_MODE=1`` is set in the process env (via
        ``assert_install_mode``).
      * The proposed payload passes the SOT invariant
        (``assert_sot_invariant``).

    Inputs:
      * collection (required, in allowlist)
      * id (required, string — typically a uuid or a content hash)
      * vector (required, list of floats)
      * payload (required, dict — must satisfy SOT invariant)
    """
    gate = assert_install_mode()
    if gate:
        return gate

    name = args.get("collection")
    err = _assert_collection(name)
    if err:
        return err

    point_id = args.get("id")
    if not point_id:
        return _err("rag.ingest.point requires 'id'")

    vector = args.get("vector")
    if not isinstance(vector, list) or not vector:
        return _err("rag.ingest.point requires 'vector': list of floats")
    if not all(isinstance(x, (int, float)) for x in vector):
        return _err("rag.ingest.point 'vector' must be numeric")

    payload = args.get("payload")
    sot = assert_sot_invariant(payload)
    if sot:
        return sot

    body = {"points": [{"id": point_id, "vector": vector, "payload": payload}]}
    data = _qdrant_put("/collections/" + str(name) + "/points", body)
    if isinstance(data, dict) and "error" in data:
        return data
    return _ok({
        "collection": name,
        "point_id": point_id,
        "qdrant_url": QDRANT_URL,
        "result": data,
    })


def _tool_rag_ingest_batch(args: Dict[str, Any], dsn: Optional[str]) -> Dict[str, Any]:
    """PUT /collections/{name}/points — batch upsert from a CSV. GATED.

    Refuses unless install mode is enabled. Each row's payload is
    checked against the SOT invariant; rows that violate it are
    skipped with a clear error in the result summary (rather than
    failing the whole batch).

    Inputs:
      * collection (required, in allowlist)
      * csv_path (required, absolute path; the bootstrap writes a
        controlled fixture at a known path — pass that path here)
      * id_column (optional, default 'point_id') — the column to use
        as the qdrant point id
      * vector_column (optional, default 'embedding_zero' — until
        the real embedder is wired in, fixtures ship a zero vector
        of the right shape)
      * payload_columns (optional, list — if omitted, every column
        except id_column and vector_column is included in the payload)
      * batch_size (optional, default 64 — Qdrant recommends <= 256)
    """
    gate = assert_install_mode()
    if gate:
        return gate

    name = args.get("collection")
    err = _assert_collection(name)
    if err:
        return err

    csv_path = args.get("csv_path")
    if not csv_path or not os.path.exists(csv_path):
        return _err("rag.ingest.batch requires existing 'csv_path', got: " + repr(csv_path))

    id_column = args.get("id_column", "point_id")
    vector_column = args.get("vector_column", "embedding_zero")
    payload_columns = args.get("payload_columns")
    batch_size = int(args.get("batch_size", 64))
    if batch_size < 1 or batch_size > 256:
        return _err("rag.ingest.batch 'batch_size' must be in [1, 256]")

    uploaded = 0
    skipped_sot = 0
    skipped_missing_id = 0
    batches = 0
    skipped_rows: List[Dict[str, Any]] = []

    try:
        with open(csv_path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                return _err("CSV at " + csv_path + " has no header")
            buf: List[Dict[str, Any]] = []
            for row_i, row in enumerate(reader):
                pid = (row.get(id_column) or "").strip()
                if not pid:
                    skipped_missing_id += 1
                    skipped_rows.append({"row": row_i, "reason": "missing " + id_column})
                    continue
                vec_str = (row.get(vector_column) or "").strip()
                if not vec_str:
                    skipped_missing_id += 1
                    skipped_rows.append({"row": row_i, "reason": "missing " + vector_column})
                    continue
                try:
                    vec = [float(x) for x in vec_str.split(",") if x.strip() != ""]
                except ValueError as e:
                    skipped_rows.append({"row": row_i, "reason": "bad vector: " + str(e)})
                    continue
                if not vec:
                    skipped_rows.append({"row": row_i, "reason": "empty vector"})
                    continue
                if payload_columns is None:
                    payload = {k: v for k, v in row.items() if k not in (id_column, vector_column)}
                else:
                    payload = {k: row.get(k) for k in payload_columns}
                # Drop empty-string values so payloads stay tidy.
                payload = {k: v for k, v in payload.items() if v not in (None, "")}
                # SOT check — skip on failure, do NOT abort the batch.
                sot = assert_sot_invariant(payload)
                if sot:
                    skipped_sot += 1
                    skipped_rows.append({
                        "row": row_i,
                        "point_id": pid,
                        "reason": sot["error"]["message"],
                    })
                    continue
                buf.append({"id": pid, "vector": vec, "payload": payload})
                if len(buf) >= batch_size:
                    data = _qdrant_put("/collections/" + str(name) + "/points", {"points": buf})
                    if isinstance(data, dict) and "error" in data:
                        return _err(
                            "batch upload failed at row " + str(row_i) + ": "
                            + str(data["error"])
                        )
                    uploaded += len(buf)
                    batches += 1
                    buf = []
            if buf:
                data = _qdrant_put("/collections/" + str(name) + "/points", {"points": buf})
                if isinstance(data, dict) and "error" in data:
                    return _err(
                        "final batch upload failed: " + str(data["error"])
                    )
                uploaded += len(buf)
                batches += 1
    except OSError as e:
        return _err("failed to read CSV at " + csv_path + ": " + type(e).__name__ + ": " + str(e))

    return _ok({
        "collection": name,
        "csv_path": csv_path,
        "qdrant_url": QDRANT_URL,
        "uploaded": uploaded,
        "batches": batches,
        "skipped_sot": skipped_sot,
        "skipped_missing_id": skipped_missing_id,
        "skipped_count": len(skipped_rows),
        "skipped_sample": skipped_rows[:10],  # cap to keep response small
    })


# ── public registry ───────────────────────────────────────────────────
#
# server.py imports TOOL_DESCRIPTORS_RAG to extend the tools/list
# response, and TOOL_REGISTRY_RAG to dispatch tools/call. Functions
# in TOOL_REGISTRY_RAG MUST match the signature ``(args: dict, dsn:
# Optional[str]) -> dict`` because server.py dispatches all tools
# uniformly as ``fn(args, DSN)``.

TOOL_DESCRIPTORS_RAG: List[Dict[str, Any]] = [
    {
        "name": "rag.health",
        "description": (
            "Liveness probe for the qdrant HTTP service. GET /healthz. "
            "Returns ok=true if qdrant responds; ok=false with an error "
            "otherwise. Read-only, never gated."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "rag.collections.list",
        "description": (
            "List all qdrant collections visible to the MCP. Read-only, "
            "never gated."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "rag.collection.info",
        "description": (
            "Get schema + point count for a single collection. "
            "Collection must be in the MCP allowlist "
            "(mpg_source_authority_documents, mpg_emails). Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"collection": {"type": "string"}},
            "required": ["collection"],
        },
    },
    {
        "name": "rag.search",
        "description": (
            "Vector similarity search against a qdrant collection. "
            "Always excludes do_not_ingest_y_n='Y' and lifecycle_status='superseded'. "
            "Collection must be in the MCP allowlist. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": {"type": "string"},
                "vector": {"type": "array", "items": {"type": "number"}},
                "limit": {"type": "integer", "minimum": 0, "maximum": 100, "default": 10},
                "filter": {"type": "object"},
                "with_payload": {"type": "boolean", "default": True},
                "with_vector": {"type": "boolean", "default": False},
            },
            "required": ["collection", "vector"],
        },
    },
    {
        "name": "rag.ingest.status",
        "description": (
            "Read the JSON marker file the bootstrap writes after a "
            "successful seed run. Read-only, never gated."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"marker_path": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "rag.ingest.point",
        "description": (
            "PUT a single point to a qdrant collection. GATED: refuses "
            "unless MCP_INSTALL_MODE=1 in env. Payload must satisfy the "
            "SOT invariant (no do_not_ingest_y_n='Y'; superseded rows "
            "must be tagged lifecycle_status='superseded')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": {"type": "string"},
                "id": {"type": "string"},
                "vector": {"type": "array", "items": {"type": "number"}},
                "payload": {"type": "object"},
            },
            "required": ["collection", "id", "vector", "payload"],
        },
    },
    {
        "name": "rag.ingest.batch",
        "description": (
            "Batch upsert points to a qdrant collection from a CSV. "
            "GATED: refuses unless MCP_INSTALL_MODE=1 in env. Each row's "
            "payload is SOT-checked; offending rows are skipped with a "
            "clear summary, never aborting the batch."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": {"type": "string"},
                "csv_path": {"type": "string"},
                "id_column": {"type": "string", "default": "point_id"},
                "vector_column": {"type": "string", "default": "embedding_zero"},
                "payload_columns": {"type": "array", "items": {"type": "string"}},
                "batch_size": {"type": "integer", "minimum": 1, "maximum": 256, "default": 64},
            },
            "required": ["collection", "csv_path"],
        },
    },
]


TOOL_REGISTRY_RAG: Dict[str, Any] = {
    "rag.health": _tool_rag_health,
    "rag.collections.list": _tool_rag_collections_list,
    "rag.collection.info": _tool_rag_collection_info,
    "rag.search": _tool_rag_search,
    "rag.ingest.status": _tool_rag_ingest_status,
    "rag.ingest.point": _tool_rag_ingest_point,
    "rag.ingest.batch": _tool_rag_ingest_batch,
}
