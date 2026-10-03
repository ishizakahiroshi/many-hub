"""Recovery checks with real process termination, without external executors."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from manyhub.config import Config
from manyhub.executors.mock import MockExecutor
from manyhub.service import Service
from manyhub.store import SQLiteStore


ROOT = Path(__file__).resolve().parents[1]
CHILD_SETUP = """
import os
import sys
sys.path.insert(0, sys.argv[2])
from manyhub.config import Config
from manyhub.executors.mock import MockExecutor
from manyhub.service import Service
from manyhub.store import SQLiteStore
store = SQLiteStore(sys.argv[1])
hub = Service(store, [MockExecutor()], clock=lambda: 1000.0)
context = Config().local_context()
"""


class AbruptRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "hub.sqlite3"
        self.context = Config().local_context()

    def tearDown(self):
        self.temp.cleanup()

    def crash_child(self, body, expected_code):
        result = subprocess.run(
            [sys.executable, "-c", CHILD_SETUP + body, str(self.path), str(ROOT)],
            cwd=ROOT, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, expected_code, result.stderr)

    def test_committed_task_survives_abrupt_exit(self):
        self.crash_child("""
hub.task_create(context, {"request_id": "durable", "input": {"text": "hello"}})
os._exit(17)
""", 17)
        store = SQLiteStore(self.path)
        try:
            hub = Service(store, [MockExecutor()], clock=lambda: 1000.0)
            task = hub.task_list(self.context)[0]
            self.assertEqual(task["state"], "queued")
            self.assertTrue(hub.run_once())
            self.assertEqual(hub.task_get(self.context, task["task_id"])["state"], "succeeded")
            self.assertFalse(hub.run_once())
        finally:
            store.close()

    def test_committed_claim_survives_abrupt_exit_without_replay(self):
        self.crash_child("""
hub.task_create(context, {"request_id": "claimed", "timeout_seconds": 1})
assert hub.claim_execution() is not None
os._exit(18)
""", 18)
        store = SQLiteStore(self.path)
        try:
            hub = Service(store, [MockExecutor()], clock=lambda: 1007.0)
            self.assertEqual(hub.recover(), 1)
            task = hub.task_list(self.context)[0]
            self.assertEqual(task["state"], "result_uncertain")
            self.assertFalse(hub.run_once())
            self.assertEqual(hub.recover(), 0)
        finally:
            store.close()

    def test_abrupt_exit_before_commit_rolls_back_whole_aggregate(self):
        self.crash_child("""
def crash_before_event(*args, **kwargs):
    os._exit(19)
hub._event = crash_before_event
hub.task_create(context, {"request_id": "uncommitted", "source_event_id": "event"})
""", 19)
        store = SQLiteStore(self.path)
        try:
            with store.transaction() as db:
                for table in ("tasks", "runs", "inbox", "outbox", "events", "rate_limits"):
                    # Table names are fixed test constants, never request data.
                    count = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    self.assertEqual(count, 0, table)
            hub = Service(store, [MockExecutor()], clock=lambda: 1000.0)
            task = hub.task_create(self.context, {"request_id": "uncommitted", "source_event_id": "event"})
            self.assertEqual(task["state"], "queued")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
