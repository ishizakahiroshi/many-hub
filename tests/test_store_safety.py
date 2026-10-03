"""Version-aware journaling, fallback transactions, and init failure cleanup."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from manyhub.config import Config
from manyhub.executors.mock import MockExecutor
from manyhub.service import Service
from manyhub.store import SQLiteStore, wal_reset_fixed


class StoreSafetyTests(unittest.TestCase):
    def test_upstream_fixed_version_boundaries(self):
        for version in [(3, 7, 0), (3, 9, 99), (3, 43, 99), (3, 44, 5), (3, 45, 99),
                        (3, 49, 99), (3, 50, 6), (3, 51, 0), (3, 51, 2)]:
            with self.subTest(version=version):
                self.assertFalse(wal_reset_fixed(version))
        for version in [(3, 44, 6), (3, 44, 9), (3, 50, 7), (3, 50, 20),
                        (3, 51, 3), (3, 52, 0), (3, 100, 0), (4, 0, 0)]:
            with self.subTest(version=version):
                self.assertTrue(wal_reset_fixed(version))
        for version in [(), (3, 51), (3, 51, True), (3, 51, -1)]:
            self.assertFalse(wal_reset_fixed(version))

    def test_unpatched_runtime_uses_extra_rollback_journal_and_retains_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hub.sqlite3"
            with mock.patch("manyhub.store.sqlite3.sqlite_version_info", (3, 51, 2)):
                store = SQLiteStore(path)
                try:
                    self.assertEqual(store.journal_mode, "delete")
                    with store.transaction() as db:
                        self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 3)
                    context = Config().local_context()
                    service = Service(store, [MockExecutor()], clock=lambda: 1000)
                    task = service.task_create(context, {"request_id": "fallback", "timeout_seconds": 1})
                    service.claim_execution()
                finally:
                    store.close()
                store = SQLiteStore(path)
                try:
                    service = Service(store, [MockExecutor()], clock=lambda: 1007)
                    self.assertEqual(service.recover(), 1)
                    self.assertEqual(service.task_get(context, task["task_id"])["state"], "result_uncertain")
                    self.assertFalse(service.run_once())
                finally:
                    store.close()

    def test_fallback_preserves_backup_and_rolls_back_failed_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hub.sqlite3"
            backup = Path(directory) / "backup.sqlite3"
            with mock.patch("manyhub.store.wal_reset_fixed", return_value=False):
                store = SQLiteStore(path)
                try:
                    service = Service(store, [MockExecutor()])
                    context = Config().local_context()
                    task = service.task_create(context, {"request_id": "fallback"})
                    with self.assertRaises(RuntimeError):
                        with store.transaction() as db:
                            db.execute("UPDATE tasks SET state='broken'")
                            raise RuntimeError("rollback")
                    self.assertEqual(service.task_get(context, task["task_id"])["state"], "queued")
                    store.backup(backup)
                finally:
                    store.close()
                restored = SQLiteStore(backup)
                try:
                    self.assertEqual(Service(restored, [MockExecutor()]).task_get(context, task["task_id"])["state"], "queued")
                finally:
                    restored.close()

    def test_initialization_failure_closes_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = mock.Mock()
            connection.execute.side_effect = sqlite3.DatabaseError("invalid database")
            with mock.patch("manyhub.store.sqlite3.connect", return_value=connection):
                with self.assertRaises(sqlite3.DatabaseError):
                    SQLiteStore(Path(directory) / "broken.sqlite3")
            connection.close.assert_called_once()

    def test_offline_existing_wal_converts_without_losing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hub.sqlite3"
            seed = sqlite3.connect(path)
            try:
                seed.execute("PRAGMA journal_mode=WAL")
                seed.execute("CREATE TABLE sentinel(value TEXT)")
                seed.execute("INSERT INTO sentinel VALUES('keep')")
                seed.commit()
            finally:
                seed.close()
            with mock.patch("manyhub.store.wal_reset_fixed", return_value=False):
                store = SQLiteStore(path)
            try:
                self.assertEqual(store.journal_mode, "delete")
                with store.transaction() as db:
                    self.assertEqual(db.execute("SELECT value FROM sentinel").fetchone()[0], "keep")
            finally:
                store.close()

    def test_active_wal_blocks_conversion_and_closes_failed_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hub.sqlite3"
            holder = sqlite3.connect(path)
            created = []
            original = sqlite3.connect
            class QuickConnection(sqlite3.Connection):
                def execute(self, sql, *args, **kwargs):
                    if sql == "PRAGMA busy_timeout=5000":
                        sql = "PRAGMA busy_timeout=1"
                    return super().execute(sql, *args, **kwargs)
            def connect(*args, **kwargs):
                connection = original(*args, **kwargs, factory=QuickConnection)
                created.append(connection)
                return connection
            try:
                holder.execute("PRAGMA journal_mode=WAL")
                holder.execute("CREATE TABLE sentinel(value TEXT)")
                holder.commit()
                holder.execute("BEGIN")
                holder.execute("SELECT * FROM sentinel").fetchall()
                with mock.patch("manyhub.store.wal_reset_fixed", return_value=False), mock.patch("manyhub.store.sqlite3.connect", side_effect=connect):
                    with self.assertRaises(sqlite3.OperationalError):
                        SQLiteStore(path)
                self.assertEqual(len(created), 1)
                with self.assertRaises(sqlite3.ProgrammingError):
                    created[0].execute("SELECT 1")
            finally:
                holder.close()

    def test_unsafe_actual_mode_return_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            original = sqlite3.connect
            created = []
            class UnsafeConnection(sqlite3.Connection):
                def execute(self, sql, *args, **kwargs):
                    if sql == "PRAGMA journal_mode=DELETE":
                        cursor = mock.Mock()
                        cursor.fetchone.return_value = ("wal",)
                        return cursor
                    return super().execute(sql, *args, **kwargs)
            def connect(*args, **kwargs):
                connection = original(*args, **kwargs, factory=UnsafeConnection)
                created.append(connection)
                return connection
            with mock.patch("manyhub.store.wal_reset_fixed", return_value=False), mock.patch("manyhub.store.sqlite3.connect", side_effect=connect):
                with self.assertRaises(ValueError):
                    SQLiteStore(Path(directory) / "hub.sqlite3")
            with self.assertRaises(sqlite3.ProgrammingError):
                created[0].execute("SELECT 1")

    def test_memory_store_remains_usable(self):
        store = SQLiteStore(":memory:")
        try:
            self.assertEqual(store.journal_mode, "memory")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
