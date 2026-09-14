#!/usr/bin/env python3
"""data-layer-adapters/mcp/server.py — universal MCP server.

Speaks the MCP protocol. Tools are framework-agnostic; tool routing
is by tool name, not by framework. Per-framework tool variants live
under mcp/tools/<framework>/ if needed, but the server itself is
universal.
"""
from __future__ import annotations
import json, os, sys

DSN = os.environ.get("DATA_LAYER_POSTGRES_DSN", "postgresql://postgres@localhost:5432/postgres")

def handle_request(req: dict) -> dict:
    method = req.get("method", "")
    if method == "initialize":
        return {"result": {"protocolVersion": "2024-11-05", "serverInfo": {"name": "data-layer", "version": "0.1.0"}}}
    if method == "tools/list":
        return {"result": {"tools": [
            {"name": "execute_sql", "description": "Run a SQL statement against the postgres data-layer.",
             "inputSchema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}}
        ]}}
    if method == "tools/call":
        params = req.get("params", {}) or {}
        name = params.get("name")
        args = params.get("arguments", {}) or {}
        if name == "execute_sql":
            sql = args.get("sql", "")
            try:
                import psycopg
                with psycopg.connect(DSN) as conn:
                    with conn.cursor() as cur:
                        cur.execute(sql)
                        if cur.description:
                            cols = [d.name for d in cur.description]
                            rows = [list(r) for r in cur.fetchall()]
                            return {"result": {"columns": cols, "rows": rows}}
                        return {"result": {"affected": cur.rowcount}}
            except Exception as e:
                return {"error": {"code": -32000, "message": str(e)}}
        return {"error": {"code": -32601, "message": f"unknown tool: {name}"}}
    return {"error": {"code": -32601, "message": f"unknown method: {method}"}}

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle_request(req)
            resp["jsonrpc"] = "2.0"
            if "id" in req:
                resp["id"] = req["id"]
            print(json.dumps(resp), flush=True)
        except Exception as e:
            print(json.dumps({"jsonrpc": "2.0", "error": {"code": -32700, "message": str(e)}}), flush=True)

if __name__ == "__main__":
    main()
