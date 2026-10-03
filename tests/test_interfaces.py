"""Real CLI processes, canonical API boundaries, and official SDK stdio client."""
import asyncio
import dataclasses
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from manyhub.auth import local_context, public_error
from manyhub.cli import read_request
from manyhub.config import Config
from manyhub.executors.mock import MockExecutor
from manyhub.model import Context, HubError
from manyhub.service import MAX_INPUT_BYTES, Service
from manyhub.store import SQLiteStore

ROOT = Path(__file__).resolve().parents[1]
HAS_MCP = importlib.util.find_spec("mcp") is not None


class InterfaceFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "hub.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *arguments, success=True):
        result = subprocess.run([sys.executable, "-m", "manyhub", "--database", str(self.database),
                                 *arguments, "--json"], cwd=ROOT, text=True, capture_output=True, timeout=15)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        return json.loads(result.stderr)

    def request_file(self, value, name="request.json"):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def service(self):
        store = SQLiteStore(self.database)
        self.addCleanup(store.close)
        return Service(store, [MockExecutor()])


class CLITests(InterfaceFixture):
    def test_discovery_and_module_entrypoint(self):
        capabilities = self.cli("capabilities")
        self.assertEqual(capabilities["name"], "MANY Hub")
        self.assertIn("task.create", capabilities["operations"])
        executors = self.cli("executor", "list")
        self.assertEqual([item["executor_id"] for item in executors], ["mock"])
        self.assertFalse(executors[0]["capabilities"]["interactive_approval"])

    def test_cli_mock_round_trip_across_processes(self):
        path = self.request_file({"request_id": "cli-create", "input": {"text": "hello"}})
        task = self.cli("task", "create", "--input-file", str(path))
        replay = self.cli("task", "create", "--input-file", str(path))
        self.assertEqual(task["task_id"], replay["task_id"])
        self.assertEqual(task["state"], "queued")
        self.assertEqual(self.cli("worker", "--once"), {"processed": True})
        self.assertEqual(self.cli("worker", "--once"), {"processed": False})
        completed = self.cli("task", "get", task["task_id"])
        self.assertEqual(completed["state"], "succeeded")
        self.assertEqual(completed["result"], {"echo": {"text": "hello"}})
        self.assertEqual(self.cli("task", "list")[0]["task_id"], task["task_id"])

    def test_reply_and_cancel(self):
        path = self.request_file({"request_id": "question", "input": {"mode": "question"}})
        task = self.cli("task", "create", "--input-file", str(path))
        self.cli("worker", "--once")
        self.assertEqual(self.cli("task", "get", task["task_id"])["state"], "awaiting_input")
        reply = self.request_file({"request_id": "answer", "input": {"answer": "42"}})
        continued = self.cli("task", "reply", task["task_id"], "--input-file", str(reply))
        self.assertNotEqual(continued["run_id"], task["run_id"])
        cancelled = self.cli("task", "cancel", task["task_id"])
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(self.cli("worker", "--once"), {"processed": False})

    def test_cli_cannot_supply_authority_or_ungranted_routes(self):
        for field in ("principal_id", "profile_id", "scopes", "approved"):
            path = self.request_file({"request_id": "bad", field: "admin:*"})
            error = self.cli("task", "create", "--input-file", str(path), success=False)
            self.assertEqual(error["error"]["code"], "invalid_request")
        for field in ("executor_id", "client_id", "transport_id"):
            path = self.request_file({"request_id": "bad", field: "ungranted"})
            error = self.cli("task", "create", "--input-file", str(path), success=False)
            self.assertEqual(error["error"]["code"], "forbidden")
        self.assertEqual(self.cli("task", "list"), [])

    def test_json_validation_and_errors_do_not_echo_input(self):
        marker = "sensitive-test-marker-do-not-echo"
        for raw in ("{", "[]", '{"request_id":"a","request_id":"b"}',
                    '{"input":{"number":NaN}}', '{"input":{"number":Infinity}}',
                    '{"request_id":"' + marker + '","principal_id":"owner"}',
                    '"' + marker + '"', '["' + marker + '"]'):
            path = self.root / "request.json"
            path.write_text(raw, encoding="utf-8")
            error = self.cli("task", "create", "--input-file", str(path), success=False)
            self.assertEqual(error["error"]["code"], "invalid_request")
            self.assertNotIn(marker, json.dumps(error))
        path.write_bytes(b" " * (MAX_INPUT_BYTES + 1))
        self.assertEqual(self.cli("task", "create", "--input-file", str(path),
                                 success=False)["error"]["code"], "limit_exceeded")
        path.write_bytes(b"\xff")
        self.assertEqual(self.cli("task", "create", "--input-file", str(path),
                                 success=False)["error"]["code"], "invalid_request")
        self.assertNotIn(marker, json.dumps(self.cli("task", "list", "--limit", marker, success=False)))

    def test_read_request_rejects_deep_json_and_missing_file(self):
        path = self.root / "input.json"
        for raw in (b"[" * 2000 + b"]" * 2000, b'{"input":{"x":"\\ud800"}}'):
            path.write_bytes(raw)
            with self.assertRaises(HubError) as error:
                read_request(path)
            self.assertEqual(error.exception.code, "invalid_request")
        with self.assertRaises(HubError):
            read_request(self.root / "absent.json")

    def test_local_serve_processes_work_and_opens_no_required_network(self):
        path = self.request_file({"request_id": "serve", "input": {"text": "background"}})
        task = self.cli("task", "create", "--input-file", str(path))
        process = subprocess.Popen([sys.executable, "-m", "manyhub", "--database", str(self.database),
                                    "serve", "--local", "--poll-interval", "0.01"], cwd=ROOT,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                completed = self.cli("task", "get", task["task_id"])
                if completed["state"] == "succeeded":
                    break
            self.assertEqual(completed["state"], "succeeded")
        finally:
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")

    def test_local_only_flags_and_validation(self):
        for args in (("serve",), ("serve", "--local", "--host", "0.0.0.0"),
                     ("mcp", "--http"), ("worker", "--poll-interval", "nan"),
                     ("worker", "--poll-interval", "0"), ("task", "list", "--limit", "101")):
            error = self.cli(*args, success=False)
            self.assertEqual(error["error"]["code"], "invalid_request")

    def test_cli_and_core_do_not_import_optional_sdk(self):
        code = """
import builtins, sys
original = builtins.__import__
def guard(name, *args, **kwargs):
    if name == 'mcp' or name.startswith('mcp.'):
        raise AssertionError('Core imported optional MCP SDK')
    return original(name, *args, **kwargs)
builtins.__import__ = guard
from manyhub.cli import main
raise SystemExit(main(['--database', sys.argv[1], 'capabilities', '--json']))
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.database)], cwd=ROOT,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["name"], "MANY Hub")

    def test_missing_mcp_dependency_reports_safe_error(self):
        code = """
import builtins, sys
original = builtins.__import__
def guard(name, *args, **kwargs):
    if name == 'mcp' or name.startswith('mcp.'):
        raise ImportError('sensitive-import-error')
    return original(name, *args, **kwargs)
builtins.__import__ = guard
from manyhub.cli import main
raise SystemExit(main(['--database', sys.argv[1], 'mcp', '--json']))
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.database)], cwd=ROOT,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(result.stderr)["error"]["code"], "unavailable")
        self.assertNotIn("sensitive-import-error", result.stderr)

    def test_local_api_ownership_and_grants_remain_canonical(self):
        hub = self.service()
        owner = local_context(Config())
        task = hub.task_create(owner, {"request_id": "api-owned"})
        other = Context("other-principal", owner.profile_id, owner.scopes)
        with self.assertRaises(HubError) as error:
            hub.task_get(other, task["task_id"])
        self.assertEqual(error.exception.code, "not_found")
        self.assertEqual(hub.task_list(other), [])
        reader = dataclasses.replace(owner, scopes=frozenset({"task:read"}))
        with self.assertRaises(HubError) as error:
            hub.task_cancel(reader, task["task_id"])
        self.assertEqual(error.exception.code, "forbidden")
        self.assertEqual(public_error(RuntimeError("secret"))["error"]["code"], "internal_error")
        self.assertNotIn("secret", json.dumps(public_error(HubError("new-secret-code", "secret"))))


@unittest.skipUnless(HAS_MCP, "Install the optional MCP extra for official SDK integration tests")
class MCPTests(InterfaceFixture):
    def test_bound_server_checks_ownership_grants_and_unknown_fields(self):
        from manyhub.adapters.mcp import create_server
        hub = self.service()
        owner = Config().local_context()
        task = hub.task_create(owner, {"request_id": "owned"})
        other = Context("different-owner", owner.profile_id, owner.scopes)
        other_server = create_server(hub, other)
        reader = dataclasses.replace(owner, scopes=frozenset({"task:read"}))
        reader_server = create_server(hub, reader)

        async def scenario():
            result = await other_server.call_tool("manyhub_task_get", {"task_id": task["task_id"]})
            self.assertTrue(result.isError)
            self.assertEqual(result.structuredContent["error"]["code"], "not_found")
            result = await reader_server.call_tool("manyhub_task_cancel", {"task_id": task["task_id"]})
            self.assertEqual(result.structuredContent["error"]["code"], "forbidden")
            result = await reader_server.call_tool("manyhub_task_create", {
                "request": {"request_id": "denied", "scopes": ["admin:*"]}})
            self.assertEqual(result.structuredContent["error"]["code"], "forbidden")
            result = await other_server.call_tool("manyhub_task_list", {"limit": "50"})
            self.assertEqual(result.structuredContent["error"]["code"], "invalid_request")
            result = await other_server.call_tool("manyhub_task_list", {"limit": True})
            self.assertEqual(result.structuredContent["error"]["code"], "invalid_request")
            result = await other_server.call_tool("manyhub_task_get", {
                "task_id": task["task_id"], "principal_id": owner.principal_id})
            self.assertEqual(result.structuredContent["error"]["code"], "invalid_request")
            result = await other_server.call_tool("complete_execution", {"status": "succeeded"})
            self.assertEqual(result.structuredContent["error"]["code"], "unsupported")
        asyncio.run(scenario())
        with self.assertRaises(HubError):
            other_server.run(transport="streamable-http")

    def test_malformed_protocol_input_is_not_logged(self):
        marker = "sensitive-test-malformed-frame"
        result = subprocess.run([sys.executable, "-m", "manyhub", "--database", str(self.database), "mcp"],
                                cwd=ROOT, input=marker + "\n", capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertIn("details withheld", result.stderr)

    def test_official_client_initializes_lists_and_calls_real_stdio_server(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        server = StdioServerParameters(command=sys.executable,
                                       args=["-m", "manyhub", "--database", str(self.database), "mcp", "--stdio"],
                                       cwd=str(ROOT))
        marker = "sensitive-validation-marker"

        async def scenario(log):
            async with stdio_client(server, errlog=log) as (read, write):
                async with ClientSession(read, write) as client:
                    initialized = await client.initialize()
                    self.assertEqual(initialized.serverInfo.name, "MANY Hub")
                    self.assertIsNotNone(initialized.capabilities.tools)
                    tools = (await client.list_tools()).tools
                    self.assertEqual({item.name for item in tools}, {
                        "manyhub_capabilities", "manyhub_executor_list", "manyhub_task_create",
                        "manyhub_task_get", "manyhub_task_list", "manyhub_task_reply", "manyhub_task_cancel"})
                    for tool in tools:
                        self.assertFalse(tool.inputSchema["additionalProperties"])
                    async def call(name, args=None):
                        result = await client.call_tool(name, args or {})
                        self.assertEqual(json.loads(result.content[0].text), result.structuredContent)
                        return result
                    capabilities = await call("manyhub_capabilities")
                    self.assertFalse(capabilities.isError)
                    executors = await call("manyhub_executor_list")
                    self.assertEqual(executors.structuredContent["executors"][0]["executor_id"], "mock")
                    request = {"request_id": "mcp-request", "input": {"mode": "question"}}
                    result = await call("manyhub_task_create", {"request": request})
                    self.assertFalse(result.isError)
                    task = result.structuredContent
                    replay = await call("manyhub_task_create", {"request": request})
                    self.assertEqual(task["task_id"], replay.structuredContent["task_id"])
                    self.assertTrue(self.cli("worker", "--once")["processed"])
                    result = await call("manyhub_task_get", {"task_id": task["task_id"]})
                    self.assertEqual(result.structuredContent["state"], "awaiting_input")
                    result = await call("manyhub_task_reply", {"task_id": task["task_id"], "request": {
                        "request_id": "mcp-reply", "input": {"answer": "42"}}})
                    self.assertFalse(result.isError)
                    self.assertTrue(self.cli("worker", "--once")["processed"])
                    result = await call("manyhub_task_get", {"task_id": task["task_id"]})
                    self.assertEqual(result.structuredContent["state"], "succeeded")
                    result = await call("manyhub_task_list", {"limit": 10})
                    self.assertEqual(len(result.structuredContent["tasks"]), 1)
                    result = await call("manyhub_task_create", {"request": {"request_id": "cancel-me"}})
                    result = await call("manyhub_task_cancel", {"task_id": result.structuredContent["task_id"]})
                    self.assertEqual(result.structuredContent["state"], "cancelled")
                    for args in ({"request": {"request_id": "attack", "principal_id": marker}},
                                 {"request": {"request_id": "attack", "scopes": ["admin:*"]}},
                                 {"request": marker}, {"request": {}, "principal_id": marker}):
                        result = await call("manyhub_task_create", args)
                        self.assertTrue(result.isError)
                        self.assertEqual(result.structuredContent["error"]["code"], "invalid_request")
                        self.assertNotIn(marker, result.content[0].text)
                    result = await call("manyhub_task_create", {"request": {
                        "request_id": "attack", "executor_id": "ungranted"}})
                    self.assertEqual(result.structuredContent["error"]["code"], "forbidden")
                    result = await call("manyhub_task_list", {"limit": "50"})
                    self.assertEqual(result.structuredContent["error"]["code"], "invalid_request")
                    await client.send_ping()
        with tempfile.TemporaryFile(mode="w+") as log:
            asyncio.run(asyncio.wait_for(scenario(log), timeout=20))
            log.seek(0)
            self.assertNotIn(marker, log.read())


if __name__ == "__main__":
    unittest.main()
