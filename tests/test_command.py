import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from manyhub.config import Config
from manyhub.executors.command import CommandExecutor, CommandSpec
from manyhub.model import ExecutionRequest
from manyhub.service import Service
from manyhub.store import SQLiteStore


@unittest.skipUnless(os.name == "posix", "POSIX process-group adapter only")
class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.python = Path(sys.executable).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def executor(self, script, **overrides):
        path = self.root / "trusted_worker.py"
        path.write_text(script)
        values = dict(executor_id="command", executable=self.python, arguments=("-I", str(path)),
                      workspace=self.root, allowed_executables=(self.python,))
        values.update(overrides)
        return CommandExecutor(CommandSpec(**values))

    def request(self, payload=None):
        return ExecutionRequest("hub", "owner", "profile", "task", "request", "run", "command", "claim",
                                payload or {"text": "hello"}, time.time() + 10, 65536)

    def test_real_full_roundtrip_never_interprets_shell_data(self):
        executor = self.executor('import json,sys\npayload=json.load(sys.stdin)\nprint(json.dumps({"status":"succeeded","output":{"echo":payload}}))')
        store = SQLiteStore(self.root / "hub.sqlite3")
        try:
            ctx = Config().local_context()
            ctx = dataclasses.replace(ctx, scopes=ctx.scopes | {"executor:use:command"})
            hub = Service(store, [executor])
            dangerous_text = "$(touch SHOULD_NOT_EXIST); rm -rf / && echo secret"
            task = hub.task_create(ctx, {"request_id": "real", "executor_id": "command", "input": {"text": dangerous_text}})
            hub.run_once()
            result = hub.task_get(ctx, task["task_id"])
            self.assertEqual(result["state"], "succeeded")
            self.assertEqual(result["result"]["echo"]["text"], dangerous_text)
            self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())
            delivery = hub.claim_delivery("local")
            hub.finish_delivery("local", delivery["outbox_id"], delivery["claim_id"], delivered=True)
            self.assertEqual(hub.task_get(ctx, task["task_id"])["deliveries"][0]["status"], "sent")
        finally:
            store.close()

    def test_clean_environment_and_fixed_working_directory(self):
        os.environ["MANYHUB_TEST_SECRET"] = "DO_NOT_LEAK"
        try:
            executor = self.executor('import os,json\nprint(json.dumps({"status":"succeeded","output":{"secret":os.environ.get("MANYHUB_TEST_SECRET"),"cwd":os.getcwd()}}))')
            result = executor.execute(self.request({"cwd": "/", "env": {"MANYHUB_TEST_SECRET": "injected"}}))
            self.assertEqual(result.status, "succeeded")
            self.assertIsNone(result.output["secret"])
            self.assertEqual(result.output["cwd"], str(self.root))
        finally:
            del os.environ["MANYHUB_TEST_SECRET"]

    def test_allowlist_and_relative_paths_rejected(self):
        with self.assertRaises(ValueError):
            self.executor('print("unused")', allowed_executables=())
        with self.assertRaises(ValueError):
            self.executor('print("unused")', executable=Path("python3"))
        with self.assertRaises(ValueError):
            self.executor('print("unused")', workspace=Path("."))

    def test_timeout_is_uncertain_and_bounded(self):
        executor = self.executor('import time\ntime.sleep(30)', timeout_seconds=0.05)
        start = time.monotonic()
        result = executor.execute(self.request())
        self.assertEqual(result.status, "result_uncertain")
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(result.output["error"], "timeout_outcome_unknown")

    def test_output_limit_includes_stderr_and_discards_contents(self):
        executor = self.executor('import sys\nsys.stderr.write("SECRET" * 100000);sys.stderr.flush()', max_output_bytes=100)
        result = executor.execute(self.request())
        self.assertEqual(result.status, "result_uncertain")
        self.assertNotIn("SECRET", json.dumps(result.output))

    def test_malformed_result_and_nonzero_exit(self):
        for script in ('print("not-json")', 'print(\'{"status":"cancelled","output":{}}\')', 'print(\'{"status":"succeeded","output":{"x":NaN}}\')'):
            with self.subTest(script=script):
                self.assertEqual(self.executor(script).execute(self.request()).status, "result_uncertain")
        result = self.executor('import sys\nsys.exit(5)').execute(self.request())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.output["exit_code"], 5)

    def test_expired_request_does_not_spawn(self):
        executor = self.executor('from pathlib import Path\nPath("spawned").touch()')
        result = executor.execute(dataclasses.replace(self.request(), deadline=time.time() - 1))
        self.assertEqual(result.status, "failed")
        self.assertFalse((self.root / "spawned").exists())

    def test_task_cannot_choose_executable_or_environment(self):
        executor = self.executor('import json\nprint(json.dumps({"status":"succeeded","output":{"fixed":True}}))')
        result = executor.execute(self.request({"executable": "/bin/sh", "argv": ["-c", "touch bad"], "env": {"PATH": "/bad"}}))
        self.assertEqual(result.output, {"fixed": True})
        self.assertFalse((self.root / "bad").exists())
        self.assertFalse(executor.capabilities.cancel_run)
        self.assertFalse(executor.capabilities.interactive_approval)

    def test_deadline_is_rechecked_after_serialization(self):
        executor = self.executor('from pathlib import Path\nPath("spawned").touch()')
        request = dataclasses.replace(self.request(), deadline=time.time() + 0.02)
        from manyhub.executors import command
        original = command.bounded_json
        def slow_encode(*args):
            time.sleep(0.03)
            return original(*args)
        with mock.patch.object(command, "bounded_json", slow_encode), mock.patch.object(command.subprocess, "Popen") as spawn:
            result = executor.execute(request)
            spawn.assert_not_called()
        self.assertEqual(result.status, "failed")

    def test_thread_start_failure_cleans_spawned_process(self):
        executor = self.executor('import time\nfrom pathlib import Path\ntime.sleep(0.1)\nPath("escaped").touch()\ntime.sleep(30)')
        from manyhub.executors import command
        processes = []
        original = command.subprocess.Popen
        def record_spawn(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        with mock.patch.object(command.subprocess, "Popen", record_spawn), mock.patch.object(command.threading.Thread, "start", side_effect=RuntimeError("failure")):
            result = executor.execute(self.request())
        self.assertEqual(result.status, "result_uncertain")
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        time.sleep(0.15)
        self.assertFalse((self.root / "escaped").exists())

    def test_duplicate_result_keys_are_rejected(self):
        executor = self.executor('print(\'{"status":"failed","status":"succeeded","output":{}}\')')
        self.assertEqual(executor.execute(self.request()).status, "result_uncertain")

    def test_cli_to_real_executor_to_cli_full_roundtrip(self):
        root = Path(__file__).resolve().parents[1]
        definition = {"executor_id": "command", "executable": str(self.python),
                      "arguments": ["-I", str(root / "examples/structured_worker.py")],
                      "workspace": str(self.root), "allowed_executables": [str(self.python)]}
        config = self.root / "command.json"
        config.write_text(json.dumps(definition))
        request = self.root / "request.json"
        request.write_text(json.dumps({"request_id": "cli-real", "executor_id": "command", "input": {"text": "shell; data only"}}))
        def cli(*arguments, configured=True):
            base = [sys.executable, "-m", "manyhub", "--database", str(self.root / "cli.sqlite3")]
            if configured:
                base += ["--command-config", str(config)]
            return subprocess.run(base + list(arguments) + ["--json"], cwd=root,
                                  capture_output=True, text=True, timeout=10)
        denied = cli("task", "create", "--input-file", str(request), configured=False)
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(json.loads(denied.stderr)["error"]["code"], "forbidden")
        created = cli("task", "create", "--input-file", str(request))
        self.assertEqual(created.returncode, 0, created.stderr)
        task_id = json.loads(created.stdout)["task_id"]
        worked = cli("worker", "--once")
        self.assertEqual(worked.returncode, 0, worked.stderr)
        self.assertTrue(json.loads(worked.stdout)["processed"])
        read = cli("task", "get", task_id)
        self.assertEqual(read.returncode, 0, read.stderr)
        result = json.loads(read.stdout)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["result"]["echo"], {"text": "shell; data only"})
        self.assertEqual(result["result"]["worker"], "structured-example")

    def test_descendant_retaining_pipes_is_stopped(self):
        script = 'import subprocess,sys,json\nsubprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"])\nprint(json.dumps({"status":"succeeded","output":{}}))'
        executor = self.executor(script)
        start = time.monotonic()
        result = executor.execute(self.request())
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(result.status, "succeeded")


if __name__ == "__main__":
    unittest.main()
