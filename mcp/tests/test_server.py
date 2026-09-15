"""Smoke tests for the universal data-layer MCP server.

Three test classes:

- SmokeTests: structural + dispatcher + protocol tests. No DB.
              Always runs.
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
    """Structural + protocol tests. No DB required."""

    @classmethod
    def setUpClass(cls):
        from tools import retrieval, safety, _registry  # noqa: F401
        cls.retrieval = retrieval
        cls.safety = safety

    def test_registry_has_exactly_14_tools(self):
        """Governance: exactly 14 read-only tools, zero write tools."""
        names = sorted(self.retrieval.TOOL_REGISTRY.keys())
        self.assertEqual(
            len(names), 14,
            "TOOL_REGISTRY must have 14 tools; got: " + str(names),
        )

    def test_descriptors_match_registry(self):
        from server import TOOL_DESCRIPTORS
        reg_names = set(self.retrieval.TOOL_REGISTRY.keys())
        desc_names = {d["name"] for d in TOOL_DESCRIPTORS}
        missing = reg_names - desc_names
        extra = desc_names - reg_names
        self.assertFalse(missing, "descriptors missing: " + str(missing))
        self.assertFalse(extra, "descriptors extra: " + str(extra))

    def test_all_descriptors_have_schema(self):
        from server import TOOL_DESCRIPTORS
        for d in TOOL_DESCRIPTORS:
            self.assertIn("name", d)
            self.assertIn("description", d)
            self.assertIn("inputSchema", d)
            self.assertEqual(d["inputSchema"]["type"], "object")
            self.assertIn("properties", d["inputSchema"])

    def test_no_write_tools_in_descriptors(self):
        """Governance: zero write tools exposed."""
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

    def test_initialize_method(self):
        from server import handle_request
        resp = handle_request({"method": "initialize", "id": 1})
        self.assertIn("result", resp)
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(resp["result"]["serverInfo"]["name"], "data-layer")

    def test_tools_list_method_returns_14(self):
        from server import handle_request
        resp = handle_request({"method": "tools/list", "id": 2})
        self.assertIn("result", resp)
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertEqual(len(names), 14, "got: " + str(names))

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
        self.assertEqual(len(tools), 14)


if __name__ == "__main__":
    unittest.main(verbosity=2)
