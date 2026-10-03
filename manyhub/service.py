"""The authoritative, transport-independent task service.

All externally reachable methods take a Context established by a trusted adapter.
Worker/result methods are internal adapter contracts, never public API tools.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from dataclasses import asdict
from typing import Any, Callable

from .model import Context, ExecutionRequest, ExecutionResult, Executor, HubError
from .store import Store, encode

TERMINAL = frozenset({"succeeded", "failed", "cancelled", "result_uncertain"})
MAX_INPUT_BYTES = 32768
MAX_OUTPUT_BYTES = 65536
MAX_HOPS = 4


def identifier(value: Any, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 200 or (not value and not empty):
        raise HubError("invalid_request", f"Invalid {field}")
    if any(ord(c) < 32 for c in value):
        raise HubError("invalid_request", f"Invalid {field}")
    return value


def bounded_json(value: Any, limit: int = MAX_INPUT_BYTES) -> str:
    if not isinstance(value, dict):
        raise HubError("invalid_request", "Expected a JSON object")
    try:
        result = encode(value)
    except (ValueError, TypeError, RecursionError):
        raise HubError("invalid_request", "Invalid JSON value") from None
    try:
        size = len(result.encode("utf-8"))
    except UnicodeEncodeError:
        raise HubError("invalid_request", "Invalid Unicode in JSON") from None
    if size > limit:
        raise HubError("limit_exceeded", "JSON value exceeds configured limit")
    return result


def digest(value: Any) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class Service:
    def __init__(self, store: Store, executors: list[Executor] | tuple[Executor, ...],
                 *, clock: Callable[[], float] = time.time, max_pending: int = 100,
                 max_requests_per_minute: int = 60):
        self.store = store
        self.executors = {e.executor_id: e for e in executors}
        if len(self.executors) != len(executors):
            raise ValueError("Executor IDs must be unique")
        self.clock = clock
        self.max_pending = max_pending
        self.max_requests_per_minute = max_requests_per_minute
        self.hub_instance_id = store.hub_instance_id

    def capabilities(self, context: Context) -> dict[str, Any]:
        return {"name": "MANY Hub", "protocol_version": "0.1", "hub_instance_id": self.hub_instance_id,
                "operations": ["capabilities", "task.create", "task.get", "task.list", "task.reply", "task.cancel", "executor.list"],
                "max_input_bytes": MAX_INPUT_BYTES, "max_output_bytes": MAX_OUTPUT_BYTES,
                "max_hops": MAX_HOPS, "remote_approval_resolution": False,
                "delivery_guarantee": "durable intent; ambiguous effects require reconciliation"}

    def executor_list(self, context: Context) -> list[dict[str, Any]]:
        context.require("executor:list")
        return [{"executor_id": key, "capabilities": asdict(executor.capabilities)}
                for key, executor in sorted(self.executors.items())
                if f"executor:use:{key}" in context.scopes or "admin:*" in context.scopes]

    def _event(self, db: Any, task_id: str, run_id: str, event_type: str, body: dict[str, Any]) -> None:
        db.execute("INSERT INTO events(event_id,task_id,run_id,event_type,created_at,body_json) VALUES(?,?,?,?,?,?)",
                   (new_id("evt"), task_id, run_id, event_type, self.clock(), encode(body)))

    @staticmethod
    def _owned(db: Any, context: Context, task_id: str) -> Any:
        row = db.execute("SELECT * FROM tasks WHERE task_id=? AND profile_id=? AND principal_id=?",
                         (task_id, context.profile_id, context.principal_id)).fetchone()
        if row is None:
            raise HubError("not_found", "Task not found")
        return row

    @staticmethod
    def _view(db: Any, row: Any) -> dict[str, Any]:
        task = {key: row[key] for key in ("task_id", "request_id", "run_id", "principal_id", "profile_id",
                "client_id", "transport_id", "executor_id", "conversation_id", "message_id", "thread_id",
                "created_at", "expires_at", "revision", "state")}
        task["input"] = json.loads(row["input_json"])
        task["result"] = json.loads(row["result_json"]) if row["result_json"] is not None else None
        task["external_refs"] = {r["namespace"]: json.loads(r["value_json"]) for r in db.execute(
            "SELECT namespace,value_json FROM external_refs WHERE task_id=?", (row["task_id"],))}
        task["deliveries"] = [{"outbox_id": r["outbox_id"], "run_id": r["run_id"], "status": r["status"]}
                              for r in db.execute("SELECT outbox_id,run_id,status FROM outbox WHERE task_id=? AND kind='delivery'",
                                                  (row["task_id"],))]
        return task

    @classmethod
    def _ack(cls, db: Any, context: Context, row: Any) -> dict[str, Any]:
        if "task:read" in context.scopes or "admin:*" in context.scopes:
            return cls._view(db, row)
        # Mutation permission does not grant read access to input/result/references.
        return {key: row[key] for key in ("task_id", "request_id", "run_id", "state", "revision")}

    def _consume_rate(self, db: Any, context: Context, now: float) -> None:
        db.execute("DELETE FROM rate_limits WHERE created_at<=?", (now - 60,))
        recent = db.execute("SELECT count(*) FROM rate_limits WHERE profile_id=? AND principal_id=?", (context.profile_id, context.principal_id)).fetchone()[0]
        if recent >= self.max_requests_per_minute:
            raise HubError("rate_limited", "Request rate exceeded")
        db.execute("INSERT INTO rate_limits VALUES(?,?,?)", (context.profile_id, context.principal_id, now))

    def task_create(self, context: Context, request: dict[str, Any]) -> dict[str, Any]:
        context.require("task:create")
        bounded_json(request)
        allowed = {"request_id", "client_id", "transport_id", "executor_id", "input", "conversation_id",
                   "message_id", "thread_id", "external_refs", "source_event_id", "origin_hub_id", "hop_count",
                   "expires_at", "timeout_seconds"}
        if set(request) - allowed:
            raise HubError("invalid_request", "Unknown task fields")
        request_id = identifier(request.get("request_id"), "request_id")
        client_id = identifier(request.get("client_id", "local"), "client_id")
        transport_id = identifier(request.get("transport_id", "local"), "transport_id")
        executor_id = identifier(request.get("executor_id", "mock"), "executor_id")
        context.require(f"executor:use:{executor_id}")
        context.require(f"client:use:{client_id}")
        context.require(f"transport:use:{transport_id}")
        executor = self.executors.get(executor_id)
        if not executor or not executor.capabilities.start_run:
            raise HubError("unsupported", "Executor cannot start a run")
        fields = {k: identifier(request.get(k, ""), k, empty=True) for k in
                  ("conversation_id", "message_id", "thread_id")}
        payload = bounded_json(request.get("input", {}))
        refs = request.get("external_refs", {})
        bounded_json(refs)
        if len(refs) > 16:
            raise HubError("limit_exceeded", "Too many external reference namespaces")
        for key in refs:
            identifier(key, "external reference namespace")
        hop_count = request.get("hop_count", 0)
        if type(hop_count) is not int or not 0 <= hop_count < MAX_HOPS:
            raise HubError("loop_detected", "Hop limit exceeded")
        origin = request.get("origin_hub_id")
        if origin is not None:
            identifier(origin, "origin_hub_id")
        if origin == self.hub_instance_id:
            raise HubError("loop_detected", "Self-originating task rejected")
        source_event_id = request.get("source_event_id")
        if source_event_id is not None:
            identifier(source_event_id, "source_event_id")
        timeout = request.get("timeout_seconds", 30)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise HubError("invalid_request", "timeout_seconds must be in (0,300]")
        now = self.clock()
        expires_at = request.get("expires_at", now + 3600)
        if type(expires_at) not in (int, float) or not math.isfinite(expires_at):
            raise HubError("invalid_request", "Invalid expires_at")
        # Fingerprint caller data, not calculated time defaults, so exact retries are stable.
        content_hash = digest(request)
        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM tasks WHERE profile_id=? AND principal_id=? AND client_id=? AND request_id=?",
                                  (context.profile_id, context.principal_id, client_id, request_id)).fetchone()
            if existing:
                if existing["request_hash"] != content_hash:
                    raise HubError("conflict", "Request ID was already used with different content")
                return self._ack(db, context, existing)
            if source_event_id:
                seen = db.execute("SELECT * FROM inbox WHERE profile_id=? AND principal_id=? AND transport_id=? AND event_id=?",
                                  (context.profile_id, context.principal_id, transport_id, source_event_id)).fetchone()
                if seen:
                    if seen["content_hash"] != content_hash:
                        raise HubError("conflict", "Event ID was already used with different content")
                    return self._ack(db, context, self._owned(db, context, seen["task_id"]))
            if expires_at <= now or expires_at > now + 86400:
                raise HubError("expired", "Expiry must be in the next 24 hours")
            count = db.execute("SELECT count(*) FROM tasks WHERE profile_id=? AND principal_id=? AND state NOT IN ('succeeded','failed','cancelled','result_uncertain')",
                               (context.profile_id, context.principal_id)).fetchone()[0]
            if count >= self.max_pending:
                raise HubError("rate_limited", "Too many pending tasks")
            self._consume_rate(db, context, now)
            task_id, run_id = new_id("task"), new_id("run")
            db.execute("INSERT OR IGNORE INTO principals VALUES(?,?)", (context.profile_id, context.principal_id))
            db.execute("""INSERT INTO tasks(task_id,profile_id,principal_id,request_id,client_id,transport_id,
                       executor_id,conversation_id,message_id,thread_id,created_at,expires_at,state,input_json,
                       request_hash,run_id,timeout_seconds) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (task_id, context.profile_id, context.principal_id, request_id, client_id, transport_id,
                        executor_id, fields["conversation_id"], fields["message_id"], fields["thread_id"], now,
                        expires_at, "queued", payload, content_hash, run_id, timeout))
            db.execute("INSERT INTO runs(run_id,task_id,state,created_at) VALUES(?,?,?,?)", (run_id, task_id, "queued", now))
            db.execute("INSERT INTO outbox(outbox_id,task_id,run_id,kind,target_id,status,created_at,payload_json) VALUES(?,?,?,'execution',?,'pending',?,?)",
                       (new_id("out"), task_id, run_id, executor_id, now, payload))
            for namespace, value in refs.items():
                db.execute("INSERT INTO external_refs VALUES(?,?,?)", (task_id, namespace, encode(value)))
            if source_event_id:
                db.execute("INSERT INTO inbox VALUES(?,?,?,?,?,?)", (context.profile_id, context.principal_id,
                           transport_id, source_event_id, content_hash, task_id))
            self._event(db, task_id, run_id, "task.created", {"hop_count": hop_count})
            return self._ack(db, context, self._owned(db, context, task_id))

    def task_get(self, context: Context, task_id: str) -> dict[str, Any]:
        context.require("task:read")
        with self.store.transaction() as db:
            return self._view(db, self._owned(db, context, task_id))

    def task_list(self, context: Context, limit: int = 50) -> list[dict[str, Any]]:
        context.require("task:read")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HubError("invalid_request", "Limit must be between 1 and 100")
        with self.store.transaction() as db:
            rows = db.execute("SELECT * FROM tasks WHERE profile_id=? AND principal_id=? ORDER BY created_at DESC, task_id LIMIT ?",
                              (context.profile_id, context.principal_id, limit)).fetchall()
            return [self._view(db, r) for r in rows]

    def task_events(self, context: Context, task_id: str) -> list[dict[str, Any]]:
        context.require("task:read")
        with self.store.transaction() as db:
            self._owned(db, context, task_id)
            return [{**dict(r), "body": json.loads(r["body_json"])} for r in db.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY seq", (task_id,))]

    def task_cancel(self, context: Context, task_id: str) -> dict[str, Any]:
        context.require("task:cancel")
        with self.store.transaction() as db:
            task = self._owned(db, context, task_id)
            if task["state"] in TERMINAL or task["state"] == "cancel_requested":
                return self._ack(db, context, task)
            stopped = task["state"] in {"queued", "awaiting_input", "awaiting_approval", "blocked"}
            state = "cancelled" if stopped else "cancel_requested"
            db.execute("UPDATE tasks SET state=?,revision=revision+1 WHERE task_id=?", (state, task_id))
            db.execute("UPDATE runs SET state=? WHERE run_id=?", (state, task["run_id"]))
            self._event(db, task_id, task["run_id"], "run.cancelled" if stopped else "run.cancel_requested", {})
            if stopped:
                # A cancelled waiting task must not later deliver a stale question.
                # Already claimed/sent notifications cannot be retracted reliably.
                db.execute("UPDATE outbox SET status='superseded' WHERE task_id=? AND kind='delivery' AND status='pending'", (task["task_id"],))
                db.execute("UPDATE outbox SET status='cancelled' WHERE run_id=? AND kind='execution' AND status='pending'", (task["run_id"],))
                self._delivery(db, task, task["run_id"], state, {})
            return self._ack(db, context, self._owned(db, context, task_id))

    def task_reply(self, context: Context, task_id: str, request: dict[str, Any]) -> dict[str, Any]:
        context.require("task:reply")
        bounded_json(request)
        if set(request) - {"request_id", "input", "expected_run_id"}:
            raise HubError("invalid_request", "Unknown reply fields")
        request_id = identifier(request.get("request_id"), "request_id")
        payload = bounded_json(request.get("input", {}))
        content_hash = digest(request)
        expected_run_id = request.get("expected_run_id")
        if expected_run_id is not None:
            identifier(expected_run_id, "expected_run_id")
        with self.store.transaction() as db:
            task = self._owned(db, context, task_id)
            context.require(f"executor:use:{task['executor_id']}")
            prior = db.execute("SELECT content_hash FROM messages WHERE task_id=? AND request_id=?", (task_id, request_id)).fetchone()
            if prior:
                if prior[0] != content_hash:
                    raise HubError("conflict", "Reply request ID reused with different content")
                return self._ack(db, context, task)
            if expected_run_id is not None and expected_run_id != task["run_id"]:
                raise HubError("conflict", "Reply belongs to a different run")
            if task["state"] == "awaiting_approval":
                raise HubError("unsupported", "Text replies cannot resolve executor approval")
            if task["state"] != "awaiting_input":
                raise HubError("conflict", "Task is not awaiting input")
            executor = self.executors.get(task["executor_id"])
            if not executor or not executor.capabilities.resume_owned_run:
                raise HubError("unsupported", "Executor cannot resume owned work")
            if task["expires_at"] <= self.clock():
                raise HubError("expired", "Task has expired")
            self._consume_rate(db, context, self.clock())
            db.execute("UPDATE outbox SET status='superseded' WHERE task_id=? AND kind='delivery' AND status='pending'", (task_id,))
            run_id = new_id("run")
            # This is an explicit continuation, never an automatic replay of a lost run.
            continuation = encode({"original_input": json.loads(task["input_json"]), "reply": json.loads(payload), "previous_run_id": task["run_id"]})
            bounded_json(json.loads(continuation))
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)", (new_id("msg"), task_id, request_id, content_hash, self.clock(), payload))
            db.execute("INSERT INTO runs(run_id,task_id,state,created_at) VALUES(?,?,'queued',?)", (run_id, task_id, self.clock()))
            db.execute("UPDATE tasks SET state='queued',run_id=?,result_json=NULL,revision=revision+1 WHERE task_id=?", (run_id, task_id))
            db.execute("INSERT INTO outbox(outbox_id,task_id,run_id,kind,target_id,status,created_at,payload_json) VALUES(?,?,?,'execution',?,'pending',?,?)",
                       (new_id("out"), task_id, run_id, task["executor_id"], self.clock(), continuation))
            self._event(db, task_id, run_id, "task.replied", {"message_request_id": request_id})
            return self._ack(db, context, self._owned(db, context, task_id))

    def _delivery(self, db: Any, task: Any, run_id: str, state: str, output: dict[str, Any]) -> None:
        payload = {"task_id": task["task_id"], "run_id": run_id, "state": state, "output": output,
                   "conversation_id": task["conversation_id"], "thread_id": task["thread_id"],
                   "message_id": task["message_id"], "origin_hub_id": self.hub_instance_id, "kind": "result"}
        db.execute("INSERT OR IGNORE INTO outbox(outbox_id,task_id,run_id,kind,target_id,status,created_at,payload_json,purpose) VALUES(?,?,?,'delivery',?,'pending',?,?,?)",
                   (new_id("out"), task["task_id"], run_id, task["transport_id"], self.clock(), encode(payload), state))

    def recover(self) -> int:
        """Expire leases without replaying possibly-effectful work. Safe to run periodically."""
        now, changed = self.clock(), 0
        with self.store.transaction() as db:
            rows = db.execute("SELECT t.*,o.outbox_id FROM tasks t JOIN outbox o ON o.run_id=t.run_id WHERE o.kind='execution' AND o.status='claimed' AND o.lease_until<=?", (now,)).fetchall()
            for task in rows:
                db.execute("UPDATE outbox SET status='uncertain',error_code='lease_expired' WHERE outbox_id=?", (task["outbox_id"],))
                db.execute("UPDATE tasks SET state='result_uncertain',revision=revision+1 WHERE task_id=?", (task["task_id"],))
                db.execute("UPDATE runs SET state='result_uncertain',completed_at=? WHERE run_id=?", (now, task["run_id"]))
                db.execute("DELETE FROM leases WHERE run_id=?", (task["run_id"],))
                self._event(db, task["task_id"], task["run_id"], "run.result_uncertain", {"reason": "lease_expired"})
                self._delivery(db, task, task["run_id"], "result_uncertain", {})
                changed += 1
            rows = db.execute("SELECT * FROM tasks WHERE state IN ('queued','awaiting_input','awaiting_approval','blocked') AND expires_at<=?", (now,)).fetchall()
            for task in rows:
                reason = "expired_before_dispatch" if task["state"] == "queued" else "expired_before_continuation"
                db.execute("UPDATE tasks SET state='failed',result_json=?,revision=revision+1 WHERE task_id=?", (encode({"error": reason}), task["task_id"]))
                db.execute("UPDATE runs SET state='failed',completed_at=? WHERE run_id=?", (now, task["run_id"]))
                db.execute("UPDATE outbox SET status='failed',error_code='expired_before_dispatch' WHERE run_id=? AND kind='execution' AND status='pending'", (task["run_id"],))
                db.execute("UPDATE outbox SET status='superseded' WHERE task_id=? AND kind='delivery' AND status='pending'", (task["task_id"],))
                self._event(db, task["task_id"], task["run_id"], "run.failed", {"reason": reason})
                self._delivery(db, task, task["run_id"], "failed", {"error": reason})
                changed += 1
            # Network delivery may have succeeded before a lost acknowledgement.
            rows = db.execute("SELECT * FROM outbox WHERE kind='delivery' AND status='claimed' AND lease_until<=?", (now,)).fetchall()
            for delivery in rows:
                db.execute("UPDATE outbox SET status='uncertain',error_code='lease_expired' WHERE outbox_id=?", (delivery["outbox_id"],))
                self._event(db, delivery["task_id"], delivery["run_id"], "delivery.failed", {"reason": "delivery_uncertain"})
                changed += 1
        return changed

    def claim_execution(self) -> ExecutionRequest | None:
        self.recover()
        with self.store.transaction() as db:
            now = self.clock()
            task = db.execute("SELECT t.*,o.outbox_id,o.payload_json FROM outbox o JOIN tasks t ON t.task_id=o.task_id WHERE o.kind='execution' AND o.status='pending' AND t.state='queued' AND t.run_id=o.run_id AND t.expires_at>? ORDER BY o.created_at,o.outbox_id LIMIT 1", (now,)).fetchone()
            if task is None:
                return None
            # Registry changes must not cause a missing executor to lose a dispatch.
            if task["executor_id"] not in self.executors:
                return None
            claim_id = new_id("claim")
            deadline = min(task["expires_at"], now + task["timeout_seconds"])
            lease_until = deadline + 5
            db.execute("UPDATE outbox SET status='claimed',claim_id=?,lease_until=? WHERE outbox_id=?", (claim_id, lease_until, task["outbox_id"]))
            db.execute("UPDATE tasks SET state='running',revision=revision+1 WHERE task_id=?", (task["task_id"],))
            db.execute("UPDATE runs SET state='running',started_at=?,deadline=?,claim_id=? WHERE run_id=?", (self.clock(), deadline, claim_id, task["run_id"]))
            db.execute("INSERT INTO leases VALUES(?,?,?)", (task["run_id"], claim_id, lease_until))
            self._event(db, task["task_id"], task["run_id"], "run.started", {})
            return ExecutionRequest(self.hub_instance_id, task["principal_id"], task["profile_id"], task["task_id"],
                task["request_id"], task["run_id"], task["executor_id"], claim_id,
                json.loads(task["payload_json"]), deadline, MAX_OUTPUT_BYTES)

    def complete_execution(self, request: ExecutionRequest, result: ExecutionResult, *, event_id: str | None = None) -> bool:
        """Fenced internal callback. Never expose this as an untrusted HTTP/MCP operation."""
        if result.status not in {"succeeded", "failed", "cancelled", "awaiting_input", "blocked", "result_uncertain"}:
            raise HubError("invalid_result", "Unsupported executor result state")
        try:
            output = bounded_json(result.output, MAX_OUTPUT_BYTES)
        except HubError:
            raise HubError("invalid_result", "Executor returned invalid or oversized output") from None
        event_id = identifier(event_id or f"complete:{request.run_id}", "event_id")
        content_hash = digest({"status": result.status, "output": result.output})
        with self.store.transaction() as db:
            row = db.execute("SELECT t.*,r.state AS run_state,r.deadline,r.claim_id AS run_claim_id,o.status AS dispatch_status FROM tasks t JOIN runs r ON r.task_id=t.task_id JOIN outbox o ON o.run_id=r.run_id AND o.kind='execution' WHERE r.run_id=?", (request.run_id,)).fetchone()
            if row is None or row["executor_id"] != request.executor_id or row["run_claim_id"] != request.claim_id or row["task_id"] != request.task_id or row["profile_id"] != request.profile_id or row["principal_id"] != request.principal_id or request.hub_instance_id != self.hub_instance_id:
                raise HubError("forbidden", "Result does not own the claimed run")
            prior = db.execute("SELECT content_hash FROM executor_inbox WHERE executor_id=? AND run_id=? AND event_id=?", (request.executor_id, request.run_id, event_id)).fetchone()
            if prior:
                if prior[0] != content_hash:
                    raise HubError("conflict", "Executor event ID reused with different result")
                return False
            db.execute("INSERT INTO executor_inbox VALUES(?,?,?,?)", (request.executor_id, request.run_id, event_id, content_hash))
            if row["run_id"] != request.run_id or row["run_state"] not in {"running", "cancel_requested"} or row["dispatch_status"] != "claimed":
                self._event(db, row["task_id"], request.run_id, "run.late_result", {"reported_state": result.status})
                return False
            state = result.status
            if self.clock() >= row["deadline"]:
                state, output = "result_uncertain", encode({"error": "deadline_exceeded"})
            executor = self.executors.get(request.executor_id)
            if state == "cancelled" and (not executor or not executor.capabilities.cancel_run):
                raise HubError("invalid_result", "Executor cannot confirm cancellation")
            db.execute("UPDATE tasks SET state=?,result_json=?,revision=revision+1 WHERE task_id=?", (state, output, request.task_id))
            db.execute("UPDATE runs SET state=?,completed_at=?,output_json=? WHERE run_id=?", (state, self.clock(), output, request.run_id))
            db.execute("UPDATE outbox SET status=? WHERE run_id=? AND kind='execution'", ("uncertain" if state == "result_uncertain" else "sent", request.run_id))
            db.execute("DELETE FROM leases WHERE run_id=?", (request.run_id,))
            event_type = {"succeeded": "run.completed", "failed": "run.failed", "cancelled": "run.cancelled", "awaiting_input": "run.question", "blocked": "run.blocked", "result_uncertain": "run.result_uncertain"}[state]
            self._event(db, request.task_id, request.run_id, event_type, {"state": state})
            self._delivery(db, row, request.run_id, state, json.loads(output))
            return True

    def run_once(self) -> bool:
        request = self.claim_execution()
        if request is None:
            return False
        try:
            if self.clock() >= request.deadline:
                self.complete_execution(request, ExecutionResult("result_uncertain", {"error": "expired_before_invocation"}))
                return True
            result = self.executors[request.executor_id].execute(request)
            self.complete_execution(request, result)
        except Exception:
            # Adapter exception text may contain secrets; do not persist/log it.
            self.complete_execution(request, ExecutionResult("result_uncertain", {"error": "executor_outcome_unknown"}), event_id=f"unknown:{request.run_id}")
        return True

    def claim_delivery(self, transport_id: str) -> dict[str, Any] | None:
        """Trusted transport worker claims only its configured route."""
        self.recover()
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM outbox WHERE kind='delivery' AND target_id=? AND status='pending' ORDER BY created_at,outbox_id LIMIT 1", (transport_id,)).fetchone()
            if row is None:
                return None
            claim_id = new_id("claim")
            db.execute("UPDATE outbox SET status='claimed',claim_id=?,lease_until=? WHERE outbox_id=?", (claim_id, self.clock() + 30, row["outbox_id"]))
            return {"outbox_id": row["outbox_id"], "claim_id": claim_id, "payload": json.loads(row["payload_json"])}

    def finish_delivery(self, transport_id: str, outbox_id: str, claim_id: str, *, delivered: bool) -> None:
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM outbox WHERE kind='delivery' AND outbox_id=? AND target_id=? AND claim_id=?", (outbox_id, transport_id, claim_id)).fetchone()
            if row is None:
                raise HubError("forbidden", "Delivery claim is not owned")
            if row["status"] != "claimed":
                return
            status = "sent" if delivered else "uncertain"
            db.execute("UPDATE outbox SET status=? WHERE outbox_id=?", (status, outbox_id))
            self._event(db, row["task_id"], row["run_id"], "delivery.completed" if delivered else "delivery.failed", {"status": status})
