"""Real process roundtrip, with operator-selected executable/argv/workspace."""
import dataclasses
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from manyhub.config import Config
from manyhub.executors.command import CommandExecutor, CommandSpec
from manyhub.service import Service
from manyhub.store import SQLiteStore


def main():
    with tempfile.TemporaryDirectory() as directory:
        store = SQLiteStore(Path(directory) / "hub.sqlite3")
        try:
            python = Path(sys.executable).resolve()
            executor = CommandExecutor(CommandSpec("example-command", python,
                ("-I", str(ROOT / "examples/structured_worker.py")), Path(directory), (python,)))
            context = Config().local_context()
            context = dataclasses.replace(context, scopes=context.scopes | {"executor:use:example-command"})
            hub = Service(store, [executor])
            task = hub.task_create(context, {"request_id": "real-process-1", "executor_id": "example-command",
                                          "thread_id": "example-thread", "input": {"message": "hello"}})
            hub.run_once()
            delivery = hub.claim_delivery("local")
            hub.finish_delivery("local", delivery["outbox_id"], delivery["claim_id"], delivered=True)
            print(json.dumps(hub.task_get(context, task["task_id"]), indent=2))
        finally:
            store.close()


if __name__ == "__main__":
    main()
