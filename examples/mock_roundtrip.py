"""Effect-free P0 acceptance demonstration; run from the repository root."""
import json
import sys
import tempfile
from pathlib import Path

# Also support `python examples/mock_roundtrip.py` in an uninstalled checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from manyhub.config import Config
from manyhub.executors.mock import MockExecutor
from manyhub.service import Service
from manyhub.store import SQLiteStore


def main():
    with tempfile.TemporaryDirectory() as directory:
        store = SQLiteStore(Path(directory) / "hub.sqlite3")
        try:
            service = Service(store, [MockExecutor()])
            context = Config().local_context()
            task = service.task_create(context, {"request_id": "demo-1", "thread_id": "demo-thread", "input": {"text": "Hello, MANY Hub"}})
            service.run_once()
            delivery = service.claim_delivery("local")
            service.finish_delivery("local", delivery["outbox_id"], delivery["claim_id"], delivered=True)
            print(json.dumps(service.task_get(context, task["task_id"]), indent=2))
        finally:
            store.close()


if __name__ == "__main__":
    main()
