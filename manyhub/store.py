"""SQLite transaction boundary. No transport or product-specific columns."""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Iterator, Protocol

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS principals(
 profile_id TEXT NOT NULL, principal_id TEXT NOT NULL,
 PRIMARY KEY(profile_id, principal_id));
CREATE TABLE IF NOT EXISTS tasks(
 task_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, principal_id TEXT NOT NULL,
 request_id TEXT NOT NULL, client_id TEXT NOT NULL, transport_id TEXT NOT NULL,
 executor_id TEXT NOT NULL, conversation_id TEXT NOT NULL, message_id TEXT NOT NULL,
 thread_id TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL,
 input_json TEXT NOT NULL, result_json TEXT, request_hash TEXT NOT NULL,
 run_id TEXT NOT NULL, timeout_seconds REAL NOT NULL,
 UNIQUE(profile_id, principal_id, client_id, request_id),
 FOREIGN KEY(profile_id,principal_id) REFERENCES principals(profile_id,principal_id));
CREATE INDEX IF NOT EXISTS tasks_owner ON tasks(profile_id,principal_id,created_at);
CREATE TABLE IF NOT EXISTS runs(
 run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
 state TEXT NOT NULL, created_at REAL NOT NULL, started_at REAL,
 completed_at REAL, deadline REAL, claim_id TEXT, output_json TEXT);
CREATE TABLE IF NOT EXISTS events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 task_id TEXT NOT NULL REFERENCES tasks(task_id), run_id TEXT NOT NULL,
 event_type TEXT NOT NULL, created_at REAL NOT NULL, body_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inbox(
 profile_id TEXT NOT NULL, principal_id TEXT NOT NULL, transport_id TEXT NOT NULL,
 event_id TEXT NOT NULL, content_hash TEXT NOT NULL, task_id TEXT NOT NULL,
 PRIMARY KEY(profile_id,principal_id,transport_id,event_id));
CREATE TABLE IF NOT EXISTS outbox(
 outbox_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
 run_id TEXT NOT NULL, kind TEXT NOT NULL, target_id TEXT NOT NULL,
 status TEXT NOT NULL, created_at REAL NOT NULL, claim_id TEXT,
 lease_until REAL, payload_json TEXT NOT NULL, error_code TEXT,
 purpose TEXT NOT NULL DEFAULT 'dispatch', UNIQUE(run_id,kind,purpose));
CREATE TABLE IF NOT EXISTS executor_inbox(
 executor_id TEXT NOT NULL, run_id TEXT NOT NULL, event_id TEXT NOT NULL,
 content_hash TEXT NOT NULL, PRIMARY KEY(executor_id,run_id,event_id));
CREATE TABLE IF NOT EXISTS external_refs(
 task_id TEXT NOT NULL REFERENCES tasks(task_id), namespace TEXT NOT NULL,
 value_json TEXT NOT NULL, PRIMARY KEY(task_id,namespace));
CREATE TABLE IF NOT EXISTS messages(
 message_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
 request_id TEXT NOT NULL, content_hash TEXT NOT NULL,
 created_at REAL NOT NULL, body_json TEXT NOT NULL, UNIQUE(task_id,request_id));
CREATE TABLE IF NOT EXISTS rate_limits(profile_id TEXT NOT NULL, principal_id TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS rates_owner ON rate_limits(profile_id,principal_id,created_at);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'Events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'Events are immutable'); END;
CREATE TABLE IF NOT EXISTS leases(
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id), claim_id TEXT NOT NULL,
 expires_at REAL NOT NULL);
"""


class Store(Protocol):
    hub_instance_id: str

    def transaction(self) -> contextlib.AbstractContextManager[sqlite3.Connection]: ...


class SQLiteStore:
    """One connection guarded in process; BEGIN IMMEDIATE coordinates processes.

    Keep the data directory private. This database can contain confidential task
    content; it deliberately does not store authentication credentials.
    """
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Exclusively create with owner-only permissions before SQLite opens it.
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, autocommit=True, check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA busy_timeout=5000")
        # The schema is atomic, and a newer schema is never silently downgraded.
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self._db.close()
            raise ValueError("Unsupported database schema version")
        self._db.executescript("BEGIN IMMEDIATE;" + SCHEMA + "PRAGMA user_version=1;COMMIT;")
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO meta VALUES('hub_instance_id',?)", (str(uuid.uuid4()),))
            self.hub_instance_id = db.execute("SELECT value FROM meta WHERE key='hub_instance_id'").fetchone()[0]

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def backup(self, destination: str | Path) -> None:
        target = Path(destination)
        if target.exists():
            raise FileExistsError("Backup destination already exists")
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with self._lock:
            backup = sqlite3.connect(target)
            try:
                self._db.backup(backup)
            finally:
                backup.close()

    def close(self) -> None:
        with self._lock:
            self._db.close()


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
