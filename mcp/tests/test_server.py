"""Smoke tests for the universal data-layer MCP server.

Three test classes plus a RAG-aware suite:

- SmokeTests: structural + dispatcher + protocol tests. No DB, no Qdrant.
              Always runs. Covers BOTH the 14 Postgres-backed tools
              and the 7 Qdrant-backed RAG tools.
- RagSmokeTests: structural tests for the RAG layer that talk to
              the live Qdrant if DATA_LAYER_QDRANT_URL is reachable.
              Skip cleanly otherwise.
- DbTests:   end-to-end against a real Postgres. Gated by
              DATA_LAYER_TEST_DSN (or DATA_LAYER_POSTGRES_DSN).
              Skipped if DSN unset or DB unreachable.
- SubprocessSmokeTests: spawns the server as a subprocess to verify
              the JSON-RPC wire protocol.
              Gated by the same DSN env vars.

Run from data-layer-adapters/:

    python3 -m unittest mcp.tests.test_server -v
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
MCP_ROOT = HERE.parent
SERVER = MCP_ROOT / "server.py"

# Make the mcp/ and tools/ packages importable regardless of cwd.
sys.path.insert(0, str(MCP_ROOT))


class SmokeTests(unittest.TestCase):
    """Structural + protocol tests. No DB or Qdrant required."""

    @classmethod
    def setUpClass(cls):
        from tools import retrieval, safety, _registry, rag  # noqa: F401
        cls.retrieval = retrieval
        cls.safety = safety
        cls.rag = rag

    def test_postgres_registry_has_exactly_14_tools(self):
        """Governance: 14 Postgres-backed read-only tools."""
        names = sorted(self.retrieval.TOOL_REGISTRY.keys())
        self.assertEqual(
            len(names), 14,
            "retrieval.TOOL_REGISTRY must have 14 tools; got: " + str(names),
        )

    def test_rag_registry_has_exactly_7_tools(self):
        """Governance: 7 Qdrant-backed RAG tools (5 read + 2 gated write)."""
        names = sorted(self.rag.TOOL_REGISTRY_RAG.keys())
        self.assertEqual(
            len(names), 7,
            "rag.TOOL_REGISTRY_RAG must have 7 tools; got: " + str(names),
        )

    def test_postgres_descriptors_match_registry(self):
        from server import TOOL_DESCRIPTORS
        reg_names = set(self.retrieval.TOOL_REGISTRY.keys())
        desc_names = {d["name"] for d in TOOL_DESCRIPTORS}
        missing = reg_names - desc_names
        extra = desc_names - reg_names
        self.assertFalse(missing, "postgres descriptors missing: " + str(missing))
        self.assertFalse(extra, "postgres descriptors extra: " + str(extra))

    def test_rag_descriptors_match_registry(self):
        from server import TOOL_DESCRIPTORS_RAG
        reg_names = set(self.rag.TOOL_REGISTRY_RAG.keys())
        desc_names = {d["name"] for d in TOOL_DESCRIPTORS_RAG}
        missing = reg_names - desc_names
        extra = desc_names - reg_names
        self.assertFalse(missing, "rag descriptors missing: " + str(missing))
        self.assertFalse(extra, "rag descriptors extra: " + str(extra))

    def test_all_descriptors_have_schema(self):
        from server import TOOL_DESCRIPTORS, TOOL_DESCRIPTORS_RAG
        for d in list(TOOL_DESCRIPTORS) + list(TOOL_DESCRIPTORS_RAG):
            self.assertIn("name", d)
            self.assertIn("description", d)
            self.assertIn("inputSchema", d)
            self.assertEqual(d["inputSchema"]["type"], "object")
            self.assertIn("properties", d["inputSchema"])

    def test_no_unbounded_write_tools_in_postgres_descriptors(self):
        """Governance: zero unconditional write tools on the postgres side.

        rag.ingest.* are allowed because they are explicitly gated by
        ``MCP_INSTALL_MODE`` (see test_rag_ingest_gated_off_by_default).
        """
        from server import TOOL_DESCRIPTORS
        names = [d["name"] for d in TOOL_DESCRIPTORS]
        forbidden = (
            "session.heartbeat",
            "seed.apply",
            "agents.create",
            "sessions.create",
            "messages.create",
            "tool_executions.create",
            "projects.create",
        )
        for bad in forbidden:
            self.assertNotIn(bad, names, "forbidden write tool present: " + bad)

    def test_rag_ingest_tools_are_marked_gated_in_descriptors(self):
        """rag.ingest.* descriptors MUST mention the install-mode gate."""
        from server import TOOL_DESCRIPTORS_RAG
        ingest = [d for d in TOOL_DESCRIPTORS_RAG if d["name"].startswith("rag.ingest.")]
        # Only rag.ingest.point and rag.ingest.batch are gated writes;
        # rag.ingest.status is a read.
        gated_writes = {"rag.ingest.point", "rag.ingest.batch"}
        for d in ingest:
            if d["name"] in gated_writes:
                self.assertIn(
                    "GATED", d["description"].upper(),
                    d["name"] + " descriptor must say it is GATED",
                )
                self.assertIn(
                    "MCP_INSTALL_MODE", d["description"],
                    d["name"] + " descriptor must mention MCP_INSTALL_MODE gate env var",
                )

    def test_per_framework_descriptors_empty(self):
        """Legacy heartbeat wrappers removed; registry empty until a future tool is added."""
        from tools._registry import FRAMEWORK_DESCRIPTORS
        self.assertEqual(
            FRAMEWORK_DESCRIPTORS, [],
            "FRAMEWORK_DESCRIPTORS must be empty",
        )

    def test_safety_guard_select_passes(self):
        """SELECT/WITH/VALUES/EXPLAIN/SHOW are accepted."""
        for sql in (
            "SELECT 1",
            "  select 1",
            "WITH x AS (SELECT 1) SELECT * FROM x",
            "VALUES (1)",
            "EXPLAIN SELECT 1",
            "SHOW search_path",
        ):
            with self.subTest(sql=sql):
                self.assertIsNone(self.safety.assert_select_only(sql), sql)

    def test_safety_guard_writes_blocked(self):
        for sql in (
            "INSERT INTO foo VALUES (1)",
            "UPDATE foo SET a=1",
            "DELETE FROM foo",
            "DROP TABLE foo",
            "CREATE TABLE x (y int)",
            "ALTER TABLE x ADD COLUMN y int",
            "TRUNCATE foo",
            "GRANT ALL ON x TO public",
            "REVOKE ALL ON x FROM public",
            "MERGE INTO x USING y ON x.a = y.a",
            "CALL my_proc()",
            "COPY foo FROM stdin",
            "VACUUM",
            "REINDEX",
            "CLUSTER",
            "REFRESH MATERIALIZED VIEW",
            "CHECKPOINT",
        ):
            with self.subTest(sql=sql):
                err = self.safety.assert_select_only(sql)
                self.assertIsNotNone(err, sql + " should be blocked")
                self.assertIn("error", err)

    def test_safety_guard_unknown_token(self):
        err = self.safety.assert_select_only("FOOBAR baz")
        self.assertIsNotNone(err)

    def test_safety_guard_empty(self):
        self.assertIsNotNone(self.safety.assert_select_only(""))
        self.assertIsNotNone(self.safety.assert_select_only("   "))
        self.assertIsNotNone(self.safety.assert_select_only("-- just a comment"))

    def test_safety_install_mode_gate_closed_by_default(self):
        """With MCP_INSTALL_MODE unset, the gate refuses rag.ingest.* calls."""
        saved = os.environ.pop("MCP_INSTALL_MODE", None)
        try:
            err = self.safety.assert_install_mode()
            self.assertIsNotNone(err, "gate must refuse when env unset")
            self.assertIn("install mode is OFF", err["error"]["message"])
        finally:
            if saved is not None:
                os.environ["MCP_INSTALL_MODE"] = saved

    def test_safety_install_mode_gate_open_when_set(self):
        """With MCP_INSTALL_MODE=1, the gate accepts rag.ingest.* calls."""
        saved = os.environ.get("MCP_INSTALL_MODE")
        os.environ["MCP_INSTALL_MODE"] = "1"
        try:
            self.assertIsNone(self.safety.assert_install_mode())
        finally:
            if saved is not None:
                os.environ["MCP_INSTALL_MODE"] = saved
            else:
                os.environ.pop("MCP_INSTALL_MODE", None)

    def test_safety_sot_invariant_refuses_do_not_ingest(self):
        """SOT invariant refuses payloads with do_not_ingest_y_n='Y'."""
        payload = {"do_not_ingest_y_n": "Y", "body": "x"}
        err = self.safety.assert_sot_invariant(payload)
        self.assertIsNotNone(err)
        self.assertIn("do_not_ingest_y_n", err["error"]["message"])

    def test_safety_sot_invariant_refuses_untagged_superseded(self):
        """SOT invariant refuses superseded rows that lack lifecycle_status tag."""
        payload = {"superseded_y_n": "Y", "body": "x"}
        err = self.safety.assert_sot_invariant(payload)
        self.assertIsNotNone(err)
        self.assertIn("superseded", err["error"]["message"])

    def test_safety_sot_invariant_accepts_tagged_superseded(self):
        """SOT invariant accepts superseded rows that are properly tagged."""
        payload = {"superseded_y_n": "Y", "lifecycle_status": "superseded", "body": "x"}
        self.assertIsNone(self.safety.assert_sot_invariant(payload))

    def test_safety_sot_invariant_accepts_clean_payload(self):
        """SOT invariant accepts payloads with no SOT flags set."""
        payload = {"body": "x", "do_not_ingest_y_n": "N", "superseded_y_n": "N"}
        self.assertIsNone(self.safety.assert_sot_invariant(payload))

    def test_safety_sot_invariant_rejects_non_dict(self):
        for bad in (None, "string", 42, ["list"]):
            with self.subTest(payload=bad):
                err = self.safety.assert_sot_invariant(bad)
                self.assertIsNotNone(err)

    def test_initialize_method(self):
        from server import handle_request
        resp = handle_request({"method": "initialize", "id": 1})
        self.assertIn("result", resp)
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(resp["result"]["serverInfo"]["name"], "data-layer")
        # Description must mention both 14 Postgres tools AND 7 RAG tools.
        desc = resp["result"]["serverInfo"]["description"]
        self.assertIn("21", desc, "initialize description must advertise 21 tools")
        self.assertIn("Qdrant", desc, "initialize description must mention Qdrant")

    def test_tools_list_method_returns_21(self):
        """Governance: tools/list returns exactly 21 descriptors (14 PG + 7 RAG)."""
        from server import handle_request
        resp = handle_request({"method": "tools/list", "id": 2})
        self.assertIn("result", resp)
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertEqual(len(names), 21, "got: " + str(names))
        # Spot-check both groups are present.
        for expected in (
            "health.check",
            "execute_sql",
            "rag.health",
            "rag.search",
            "rag.ingest.point",
            "rag.ingest.batch",
        ):
            self.assertIn(expected, names, "missing tool: " + expected)

    def test_unknown_method_returns_error(self):
        from server import handle_request
        resp = handle_request({"method": "bogus/method", "id": 3})
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_unknown_tool_call_returns_error(self):
        from server import handle_request
        resp = handle_request({
            "method": "tools/call",
            "params": {"name": "bogus.tool", "arguments": {}},
            "id": 4,
        })
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_execute_sql_with_write_returns_error_without_db(self):
        """execute_sql refuses DROP / INSERT at the structural layer, no DB needed."""
        from server import handle_request
        for bad in ("DROP TABLE foo", "INSERT INTO x VALUES (1)",
                    "UPDATE x SET a=1", "DELETE FROM x"):
            with self.subTest(sql=bad):
                resp = handle_request({
                    "method": "tools/call",
                    "params": {"name": "execute_sql", "arguments": {"sql": bad}},
                    "id": 5,
                })
                self.assertIn("error", resp, bad)
                self.assertIn(
                    "refused", resp["error"]["message"].lower(),
                    bad,
                )

    def test_rag_ingest_point_gated_off_by_default(self):
        """rag.ingest.point refuses when MCP_INSTALL_MODE is unset."""
        saved = os.environ.pop("MCP_INSTALL_MODE", None)
        try:
            from server import handle_request
            resp = handle_request({
                "method": "tools/call",
                "params": {
                    "name": "rag.ingest.point",
                    "arguments": {
                        "collection": "mpg_source_authority_documents",
                        "id": "x",
                        "vector": [0.0] * 768,
                        "payload": {"body": "x"},
                    },
                },
                "id": 6,
            })
            self.assertIn("error", resp)
            self.assertIn("install mode is OFF", resp["error"]["message"])
        finally:
            if saved is not None:
                os.environ["MCP_INSTALL_MODE"] = saved

    def test_rag_ingest_batch_gated_off_by_default(self):
        """rag.ingest.batch refuses when MCP_INSTALL_MODE is unset."""
        saved = os.environ.pop("MCP_INSTALL_MODE", None)
        try:
            from server import handle_request
            resp = handle_request({
                "method": "tools/call",
                "params": {
                    "name": "rag.ingest.batch",
                    "arguments": {
                        "collection": "mpg_source_authority_documents",
                        "csv_path": "/nonexistent",
                    },
                },
                "id": 7,
            })
            self.assertIn("error", resp)
            self.assertIn("install mode is OFF", resp["error"]["message"])
        finally:
            if saved is not None:
                os.environ["MCP_INSTALL_MODE"] = saved

    def test_rag_search_collection_allowlist_rejects_unknown(self):
        """rag.search refuses collection names outside the allowlist."""
        from server import handle_request
        resp = handle_request({
            "method": "tools/call",
            "params": {
                "name": "rag.search",
                "arguments": {
                    "collection": "some_random_collection",
                    "vector": [0.0] * 768,
                },
            },
            "id": 8,
        })
        self.assertIn("error", resp)
        self.assertIn("allowlist", resp["error"]["message"])

    def test_rag_collection_info_allowlist_rejects_unknown(self):
        """rag.collection.info refuses collection names outside the allowlist."""
        from server import handle_request
        resp = handle_request({
            "method": "tools/call",
            "params": {
                "name": "rag.collection.info",
                "arguments": {"collection": "some_random_collection"},
            },
            "id": 9,
        })
        self.assertIn("error", resp)
        self.assertIn("allowlist", resp["error"]["message"])


class RagSmokeTests(unittest.TestCase):
    """Structural + functional RAG tests that talk to the live Qdrant.

    Gated by DATA_LAYER_QDRANT_URL (default http://localhost:6333).
    Skip cleanly if Qdrant is unreachable.
    """

    @classmethod
    def setUpClass(cls):
        cls.url = os.environ.get(
            "DATA_LAYER_QDRANT_URL", "http://localhost:6333"
        ).rstrip("/")
        try:
            import requests
            r = requests.get(cls.url + "/healthz", timeout=3)
            if r.status_code != 200:
                raise RuntimeError(
                    "qdrant returned " + str(r.status_code) + " at " + cls.url
                )
        except Exception as e:
            raise unittest.SkipTest(
                "Qdrant unreachable at " + cls.url + ": " + str(e)
            )

    def test_rag_health_ok(self):
        from server import handle_request
        resp = handle_request({
            "method": "tools/call",
            "params": {"name": "rag.health", "arguments": {}},
            "id": 1,
        })
        self.assertIn("result", resp, repr(resp))
        self.assertTrue(resp["result"]["ok"])

    def test_rag_collections_list_returns_list(self):
        from server import handle_request
        resp = handle_request({
            "method": "tools/call",
            "params": {"name": "rag.collections.list", "arguments": {}},
            "id": 1,
        })
        self.assertIn("result", resp, repr(resp))
        self.assertIn("collections", resp["result"])

    def test_rag_collection_info_on_allowlisted_collection(self):
        from server import handle_request
        resp = handle_request({
            "method": "tools/call",
            "params": {
                "name": "rag.collection.info",
                "arguments": {"collection": "mpg_source_authority_documents"},
            },
            "id": 1,
        })
        # Two outcomes are acceptable: the collection exists (result
        # with config) OR the migration has not run yet (error 404).
        # What must NOT happen is an allowlist refusal or a transport
        # error.
        self.assertTrue(
            "result" in resp or "error" in resp,
            "no result and no error: " + repr(resp),
        )
        if "error" in resp:
            # If there is an error, it must be the qdrant "not found" one,
            # not an allowlist refusal or a transport error.
            self.assertNotIn("allowlist", resp["error"]["message"])
            self.assertNotIn("unreachable", resp["error"]["message"])


class DbTests(unittest.TestCase):
    """End-to-end tests against a real Postgres; skip if DSN unreachable."""

    @classmethod
    def setUpClass(cls):
        cls.dsn = (
            os.environ.get("DATA_LAYER_TEST_DSN")
            or os.environ.get("DATA_LAYER_POSTGRES_DSN")
        )
        if not cls.dsn:
            raise unittest.SkipTest("DATA_LAYER_TEST_DSN unset")
        try:
            import psycopg
            with psycopg.connect(cls.dsn, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
        except Exception as e:
            raise unittest.SkipTest(
                "Postgres unreachable at " + cls.dsn + ": " + str(e)
            )

    def setUp(self):
        import server as srv
        self._orig_dsn = srv.DSN
        srv.DSN = self.dsn

    def tearDown(self):
        import server as srv
        srv.DSN = self._orig_dsn

    def test_health_check(self):
        import server as srv
        resp = srv.handle_request({
            "method": "tools/call",
            "params": {"name": "health.check", "arguments": {}},
            "id": 1,
        })
        self.assertIn("result", resp, "health.check failed: " + repr(resp))
        payload = resp["result"]
        self.assertIn("ok", payload)
        self.assertIn("tables", payload)
        for t in ("projects", "messages", "tool_executions"):
            self.assertIn(t, payload["tables"])

    def test_projects_list(self):
        import server as srv
        resp = srv.handle_request({
            "method": "tools/call",
            "params": {"name": "projects.list", "arguments": {"limit": 10}},
            "id": 1,
        })
        self.assertIn("result", resp, "projects.list failed: " + repr(resp))
        self.assertIn("rows", resp["result"])

    def test_execute_sql_select_works(self):
        import server as srv
        resp = srv.handle_request({
            "method": "tools/call",
            "params": {
                "name": "execute_sql",
                "arguments": {"sql": "SELECT 1 AS one, 2 AS two"},
            },
            "id": 1,
        })
        self.assertIn("result", resp, "execute_sql SELECT failed: " + repr(resp))
        self.assertIn("rows", resp["result"])
        self.assertEqual(resp["result"]["rows"][0]["one"], 1)

    def test_execute_sql_insert_rejected_before_db_open(self):
        """Safety guard refuses INSERT without ever opening a cursor."""
        import server as srv
        resp = srv.handle_request({
            "method": "tools/call",
            "params": {
                "name": "execute_sql",
                "arguments": {
                    "sql": (
                        "INSERT INTO projects (project_key, display_name) "
                        "VALUES ('_smoke_should_not_persist', '_smoke')"
                    ),
                },
            },
            "id": 1,
        })
        self.assertIn("error", resp, "INSERT must be rejected")
        # Confirm the row did NOT land in the DB.
        check = srv.handle_request({
            "method": "tools/call",
            "params": {
                "name": "execute_sql",
                "arguments": {
                    "sql": (
                        "SELECT count(*) FROM projects "
                        "WHERE project_key = '_smoke_should_not_persist'"
                    ),
                },
            },
            "id": 2,
        })
        self.assertIn("result", check, repr(check))
        self.assertEqual(check["result"]["rows"][0]["count"], 0)


class SubprocessSmokeTests(unittest.TestCase):
    """Verify the JSON-RPC wire protocol via subprocess spawn."""

    @classmethod
    def setUpClass(cls):
        cls.dsn = (
            os.environ.get("DATA_LAYER_TEST_DSN")
            or os.environ.get("DATA_LAYER_POSTGRES_DSN")
        )
        if not cls.dsn:
            raise unittest.SkipTest("DATA_LAYER_TEST_DSN unset")
        try:
            import psycopg
            with psycopg.connect(cls.dsn, connect_timeout=3):
                pass
        except Exception as e:
            raise unittest.SkipTest(
                "Postgres unreachable at " + cls.dsn + ": " + str(e)
            )

    def test_handshake(self):
        """Spawn the server, send initialize + tools/list, get responses."""
        env = os.environ.copy()
        env["DATA_LAYER_POSTGRES_DSN"] = self.dsn
        proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(MCP_ROOT),
        )
        try:
            init = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
            lst = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
            payload = json.dumps(init) + "\n" + json.dumps(lst) + "\n"
            out, _ = proc.communicate(input=payload.encode(), timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
        lines = [
            json.loads(l)
            for l in out.decode().strip().split("\n")
            if l.strip()
        ]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["result"]["protocolVersion"], "2024-11-05")
        tools = lines[1]["result"]["tools"]
        self.assertEqual(len(tools), 21)


if __name__ == "__main__":
    unittest.main(verbosity=2)
