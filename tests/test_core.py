import contextlib
import dataclasses
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from manyhub.config import Config
from manyhub.executors.mock import MockExecutor
from manyhub.model import Capabilities, Context, ExecutionResult, HubError
from manyhub.service import Service
from manyhub.store import SQLiteStore


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "hub.sqlite3"
        self.now = 1700000000.0
        self.store = SQLiteStore(self.path)
        self.hub = Service(self.store, [MockExecutor()], clock=lambda: self.now)
        self.ctx = Config().local_context()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def create(self, **fields):
        return self.hub.task_create(self.ctx, {"request_id": "request-1", "input": {"text": "hello"}, **fields})

    def state(self, task):
        return self.hub.task_get(self.ctx, task["task_id"])["state"]

    def assert_code(self, code, fn, *args, **kwargs):
        with self.assertRaises(HubError) as error:
            fn(*args, **kwargs)
        self.assertEqual(error.exception.code, code)

    def test_mock_round_trip_and_delivery_are_independent(self):
        task = self.create(thread_id="thread-1")
        self.assertEqual(task["state"], "queued")
        self.assertNotEqual(task["task_id"], task["run_id"])
        self.assertNotEqual(task["request_id"], task["run_id"])
        self.assertTrue(self.hub.run_once())
        result = self.hub.task_get(self.ctx, task["task_id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["result"], {"echo": {"text": "hello"}})
        self.assertEqual(result["deliveries"][0]["status"], "pending")
        delivery = self.hub.claim_delivery("local")
        self.assertEqual(delivery["payload"]["thread_id"], "thread-1")
        self.hub.finish_delivery("local", delivery["outbox_id"], delivery["claim_id"], delivered=True)
        self.assertEqual(self.hub.task_get(self.ctx, task["task_id"])["deliveries"][0]["status"], "sent")
        self.assertFalse(self.hub.run_once())

    def test_request_and_event_replay_do_not_execute_twice(self):
        task = self.create(source_event_id="event-1")
        self.assertEqual(self.create(source_event_id="event-1")["task_id"], task["task_id"])
        self.hub.run_once()
        self.assertEqual(self.create(source_event_id="event-1")["task_id"], task["task_id"])
        self.assertFalse(self.hub.run_once())
        self.assertEqual(len(self.hub.task_list(self.ctx)), 1)
        self.assert_code("conflict", self.create, input={"text": "different"}, source_event_id="event-1")
        self.assert_code("conflict", self.create, request_id="request-2", source_event_id="event-1")

    def test_replay_after_expiry_returns_existing_task(self):
        task = self.create(expires_at=self.now + 1)
        self.now += 2
        self.assertEqual(self.create(expires_at=self.now - 1)["task_id"], task["task_id"])

    def test_queued_work_recovers_after_reopening(self):
        task = self.create()
        instance = self.hub.hub_instance_id
        self.store.close()
        self.store = SQLiteStore(self.path)
        self.hub = Service(self.store, [MockExecutor()], clock=lambda: self.now)
        self.assertEqual(instance, self.hub.hub_instance_id)
        self.assertTrue(self.hub.run_once())
        self.assertEqual(self.state(task), "succeeded")

    def test_claimed_work_never_automatically_replays_after_crash(self):
        task = self.create(timeout_seconds=1)
        request = self.hub.claim_execution()
        self.store.close()
        self.store = SQLiteStore(self.path)
        self.hub = Service(self.store, [MockExecutor()], clock=lambda: self.now)
        self.assertEqual(self.hub.recover(), 0)
        self.now += 7
        self.assertEqual(self.hub.recover(), 1)
        self.assertEqual(self.state(task), "result_uncertain")
        self.assertFalse(self.hub.run_once())
        self.assertFalse(self.hub.complete_execution(request, ExecutionResult("succeeded", {"late": True})))
        self.assertEqual(self.state(task), "result_uncertain")
        self.assertIn("run.late_result", [e["event_type"] for e in self.hub.task_events(self.ctx, task["task_id"])])

    def test_late_result_after_deadline_is_uncertain(self):
        task = self.create(timeout_seconds=1)
        request = self.hub.claim_execution()
        self.now += 2
        self.hub.complete_execution(request, ExecutionResult("succeeded", {}))
        self.assertEqual(self.state(task), "result_uncertain")

    def test_queued_expiry_never_calls_executor(self):
        task = self.create(expires_at=self.now + 1)
        self.now += 2
        self.assertFalse(self.hub.run_once())
        self.assertEqual(self.state(task), "failed")

    def test_cancel_queued_and_running_are_distinct(self):
        queued = self.create()
        self.assertEqual(self.hub.task_cancel(self.ctx, queued["task_id"])["state"], "cancelled")
        self.assertFalse(self.hub.run_once())
        running = self.create(request_id="second")
        request = self.hub.claim_execution()
        self.assertEqual(self.hub.task_cancel(self.ctx, running["task_id"])["state"], "cancel_requested")
        # Requesting cancellation is not proof that an effect was stopped.
        self.hub.complete_execution(request, ExecutionResult("succeeded", {}))
        self.assertEqual(self.state(running), "succeeded")

    def test_question_reply_is_owned_explicit_continuation(self):
        task = self.create(input={"mode": "question"})
        self.hub.run_once()
        self.assertEqual(self.state(task), "awaiting_input")
        request = {"request_id": "reply-1", "input": {"answer": "42", "approved": True}}
        reply = self.hub.task_reply(self.ctx, task["task_id"], request)
        self.assertNotEqual(reply["run_id"], task["run_id"])
        self.assertEqual(self.hub.task_reply(self.ctx, task["task_id"], request)["run_id"], reply["run_id"])
        self.assert_code("conflict", self.hub.task_reply, self.ctx, task["task_id"], {"request_id": "reply-1", "input": {}})
        self.hub.run_once()
        self.assertEqual(self.state(task), "succeeded")
        self.assertFalse(self.hub.executor_list(self.ctx)[0]["capabilities"]["interactive_approval"])

    def test_ownership_and_scope_are_independent(self):
        task = self.create()
        other = Context("someone-else", self.ctx.profile_id, self.ctx.scopes)
        self.assert_code("not_found", self.hub.task_get, other, task["task_id"])
        self.assert_code("not_found", self.hub.task_cancel, other, task["task_id"])
        other_profile = Context(self.ctx.principal_id, "other-profile", self.ctx.scopes)
        self.assertEqual(self.hub.task_list(other_profile), [])
        read_only = Context(self.ctx.principal_id, self.ctx.profile_id, frozenset({"task:read"}))
        self.assertEqual(self.hub.task_get(read_only, task["task_id"])["task_id"], task["task_id"])
        self.assert_code("forbidden", self.hub.task_cancel, read_only, task["task_id"])
        self.assert_code("forbidden", self.hub.task_create, read_only, {"request_id": "new"})

    def test_caller_cannot_inject_principal_grants_or_transport(self):
        for field in ("principal_id", "profile_id", "scopes", "approved"):
            self.assert_code("invalid_request", self.create, **{field: "admin"})
        self.assert_code("forbidden", self.create, transport_id="ungranted-route")
        self.assert_code("forbidden", self.create, client_id="ungranted-client")
        self.assert_code("forbidden", self.create, executor_id="ungranted-executor")

    def test_executor_result_must_be_fenced_to_claim_and_owner(self):
        task = self.create()
        request = self.hub.claim_execution()
        result = ExecutionResult("succeeded", {})
        for field in ("claim_id", "executor_id", "task_id", "profile_id", "principal_id", "hub_instance_id"):
            self.assert_code("forbidden", self.hub.complete_execution, dataclasses.replace(request, **{field: "fake"}), result)
        self.assertEqual(self.state(task), "running")
        self.assertTrue(self.hub.complete_execution(request, result))
        self.assertFalse(self.hub.complete_execution(request, result))
        self.assert_code("conflict", self.hub.complete_execution, request, ExecutionResult("failed", {}))

    def test_limits_expiry_loop_and_json_validation(self):
        self.assert_code("loop_detected", self.create, origin_hub_id=self.hub.hub_instance_id)
        self.assert_code("loop_detected", self.create, hop_count=4)
        self.assert_code("loop_detected", self.create, hop_count=True)
        self.assert_code("expired", self.create, expires_at=self.now - 1)
        self.assert_code("expired", self.create, expires_at=self.now + 90000)
        self.assert_code("invalid_request", self.create, timeout_seconds=0)
        self.assert_code("invalid_request", self.create, timeout_seconds=float("nan"))
        self.assert_code("invalid_request", self.create, input={"bad": float("nan")})
        self.assert_code("invalid_request", self.create, input={"bad": "\ud800"})
        self.assert_code("limit_exceeded", self.create, input={"big": "x" * 40000})

    def test_backpressure_and_persistent_rate_limit(self):
        self.hub.max_pending = 1
        self.create()
        self.assert_code("rate_limited", self.create, request_id="another")
        self.hub.run_once()
        self.hub.max_requests_per_minute = 1
        self.assert_code("rate_limited", self.create, request_id="another")
        self.now += 61
        self.create(request_id="another")

    def test_ambiguous_delivery_is_not_resent(self):
        task = self.create()
        self.hub.run_once()
        delivery = self.hub.claim_delivery("local")
        self.assert_code("forbidden", self.hub.finish_delivery, "other", delivery["outbox_id"], delivery["claim_id"], delivered=True)
        self.now += 31
        self.hub.recover()
        self.assertIsNone(self.hub.claim_delivery("local"))
        task = self.hub.task_get(self.ctx, task["task_id"])
        self.assertEqual(task["state"], "succeeded")
        self.assertEqual(task["deliveries"][0]["status"], "uncertain")

    def test_adapter_exception_never_leaks_exception_text(self):
        class Broken(MockExecutor):
            def execute(self, request):
                raise RuntimeError("SECRET_VALUE_DO_NOT_STORE")
        self.hub.executors["mock"] = Broken()
        task = self.create()
        self.hub.run_once()
        self.assertEqual(self.state(task), "result_uncertain")
        text = json.dumps(self.hub.task_get(self.ctx, task["task_id"])) + json.dumps(self.hub.task_events(self.ctx, task["task_id"]))
        self.assertNotIn("SECRET_VALUE_DO_NOT_STORE", text)

    def test_invalid_or_oversized_result_is_uncertain(self):
        class Broken(MockExecutor):
            def execute(self, request):
                return ExecutionResult("succeeded", {"big": "x" * 70000})
        self.hub.executors["mock"] = Broken()
        task = self.create()
        self.hub.run_once()
        self.assertEqual(self.state(task), "result_uncertain")

    def test_backup_and_schema_version_guard(self):
        task = self.create(external_refs={"example-system": {"item": "opaque-123"}})
        backup_path = Path(self.temp.name) / "backup.sqlite3"
        self.store.backup(backup_path)
        restored = SQLiteStore(backup_path)
        try:
            hub = Service(restored, [MockExecutor()], clock=lambda: self.now)
            self.assertEqual(hub.task_get(self.ctx, task["task_id"])["external_refs"], {"example-system": {"item": "opaque-123"}})
        finally:
            restored.close()
        with self.assertRaises(FileExistsError):
            self.store.backup(backup_path)
        with contextlib.closing(sqlite3.connect(backup_path)) as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaises(ValueError):
            SQLiteStore(backup_path)

    def test_events_are_immutable_and_transactions_roll_back(self):
        task = self.create()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction() as db:
                db.execute("DELETE FROM events")
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as db:
                db.execute("UPDATE tasks SET state='broken' WHERE task_id=?", (task["task_id"],))
                raise RuntimeError("abort")
        self.assertEqual(self.state(task), "queued")

    def test_mutation_permission_does_not_disclose_task_content(self):
        task = self.create(external_refs={"private": {"id": "secret"}})
        self.hub.run_once()
        scopes = self.ctx.scopes - {"task:read"}
        write_only = dataclasses.replace(self.ctx, scopes=scopes)
        for response in (
            self.hub.task_cancel(write_only, task["task_id"]),
            self.hub.task_create(write_only, {"request_id": "request-1", "input": {"text": "hello"}, "external_refs": {"private": {"id": "secret"}}}),
        ):
            self.assertEqual(set(response), {"task_id", "request_id", "run_id", "state", "revision"})
        question = self.create(request_id="question", input={"mode": "question"})
        self.hub.run_once()
        response = self.hub.task_reply(write_only, question["task_id"], {"request_id": "reply", "input": {"answer": "yes"}})
        self.assertNotIn("input", response)
        self.assertNotIn("result", response)
        self.assertNotIn("external_refs", response)

    def test_cancel_waiting_question_delivers_cancellation_not_stale_question(self):
        task = self.create(input={"mode": "question"})
        self.hub.run_once()
        self.hub.task_cancel(self.ctx, task["task_id"])
        delivery = self.hub.claim_delivery("local")
        self.assertEqual(delivery["payload"]["state"], "cancelled")
        self.assertIsNone(self.hub.claim_delivery("local"))
        self.assertEqual(sorted(d["status"] for d in self.hub.task_get(self.ctx, task["task_id"])["deliveries"]), ["claimed", "superseded"])

    def test_expiry_between_recovery_and_claim_is_checked_atomically(self):
        self.create(expires_at=self.now + 1)
        recover = self.hub.recover
        def delayed_recover():
            result = recover()
            self.now += 2
            return result
        self.hub.recover = delayed_recover
        self.assertIsNone(self.hub.claim_execution())

    def test_expiry_between_claim_and_invocation_never_calls_executor(self):
        class MustNotRun(MockExecutor):
            called = False
            def execute(self, request):
                self.called = True
                return super().execute(request)
        executor = MustNotRun()
        self.hub.executors["mock"] = executor
        self.create(expires_at=self.now + 1)
        claim = self.hub.claim_execution
        def delayed_claim():
            result = claim()
            self.now += 2
            return result
        self.hub.claim_execution = delayed_claim
        self.hub.run_once()
        self.assertFalse(executor.called)

    def test_reply_cannot_bypass_new_run_rate_limit(self):
        self.hub.max_requests_per_minute = 1
        task = self.create(input={"mode": "question"})
        self.hub.run_once()
        reply = {"request_id": "reply", "input": {"answer": "42"}}
        self.assert_code("rate_limited", self.hub.task_reply, self.ctx, task["task_id"], reply)
        self.now += 61
        acknowledged = self.hub.task_reply(self.ctx, task["task_id"], reply)
        self.assertEqual(acknowledged["state"], "queued")
        self.assertEqual(self.hub.task_reply(self.ctx, task["task_id"], reply)["run_id"], acknowledged["run_id"])

    def test_expired_waiting_tasks_do_not_exhaust_pending_quota(self):
        self.hub.max_pending = 1
        task = self.create(input={"mode": "question"}, expires_at=self.now + 1)
        self.hub.run_once()
        self.now += 2
        self.assertEqual(self.hub.recover(), 1)
        self.assertEqual(self.state(task), "failed")
        delivery = self.hub.claim_delivery("local")
        self.assertEqual(delivery["payload"]["state"], "failed")
        self.create(request_id="new")

    def test_reply_and_cancel_supersede_previous_run_question(self):
        task = self.create(input={"mode": "question"})
        self.hub.run_once()
        self.hub.task_reply(self.ctx, task["task_id"], {"request_id": "reply", "input": {"answer": "42"}})
        self.assertIsNone(self.hub.claim_delivery("local"))
        self.hub.task_cancel(self.ctx, task["task_id"])
        self.assertEqual(self.hub.claim_delivery("local")["payload"]["state"], "cancelled")

    def test_reply_can_be_bound_to_expected_run(self):
        task = self.create(input={"mode": "question"})
        self.hub.run_once()
        self.assert_code("conflict", self.hub.task_reply, self.ctx, task["task_id"],
                         {"request_id": "stale", "expected_run_id": "wrong", "input": {}})
        request = {"request_id": "reply", "expected_run_id": task["run_id"], "input": {"answer": "42"}}
        next_run = self.hub.task_reply(self.ctx, task["task_id"], request)
        self.assertEqual(self.hub.task_reply(self.ctx, task["task_id"], request)["run_id"], next_run["run_id"])
        self.assert_code("conflict", self.hub.task_reply, self.ctx, task["task_id"],
                         {"request_id": "other-reply", "expected_run_id": task["run_id"], "input": {}})

    def test_two_worker_connections_only_one_claim(self):
        self.create()
        other_store = SQLiteStore(self.path)
        other = Service(other_store, [MockExecutor()], clock=lambda: self.now)
        barrier = threading.Barrier(2)
        results = []
        errors = []
        def claim(service):
            try:
                barrier.wait()
                results.append(service.claim_execution())
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=claim, args=(hub,)) for hub in (self.hub, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        other_store.close()
        self.assertFalse(errors)
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_two_clients_concurrently_dedupe_request(self):
        other_store = SQLiteStore(self.path)
        other = Service(other_store, [MockExecutor()], clock=lambda: self.now)
        barrier = threading.Barrier(2)
        ids, errors = [], []
        def create(service):
            try:
                barrier.wait()
                ids.append(service.task_create(self.ctx, {"request_id": "same"})["task_id"])
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=create, args=(hub,)) for hub in (self.hub, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        other_store.close()
        self.assertFalse(errors)
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 1)


if __name__ == "__main__":
    unittest.main()
