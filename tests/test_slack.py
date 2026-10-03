"""Complete offline P2 fixtures. No token, Socket connection, or Slack post."""
import copy
import dataclasses
import json
import importlib.util
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from manyhub.adapters.slack import SlackAdapter, SlackConfig, SocketModeRuntime
from manyhub.executors.mock import MockExecutor
from manyhub.model import Context, ExecutionResult, HubError
from manyhub.service import Service
from manyhub.store import SQLiteStore


class FakeSlackAPI:
    def __init__(self):
        self.posts = []
        self.failure = False
        self.response = None

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        if self.failure:
            # Simulates a real effect followed by lost acknowledgement.
            raise RuntimeError("xoxb-SECRET_BODY_MUST_NOT_LEAK")
        return self.response or {"ok": True, "channel": kwargs["channel"], "ts": f"1700000000.{len(self.posts) * 2:06d}"}


class QuestionExecutor(MockExecutor):
    def execute(self, request):
        if "reply" not in request.payload:
            return ExecutionResult("awaiting_input", {"question": "Which target?"})
        return super().execute(request)


class SlackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "hub.sqlite3"
        self.now = 1700000000.0
        self.ctx = Context("owner", "profile", frozenset({
            "task:create", "task:read", "task:reply", "task:cancel",
            "client:use:slack", "transport:use:slack", "executor:use:mock"}))
        self.config = SlackConfig(team_id="T123", app_id="A123", bot_user_id="UBOT",
                                  channel_ids=frozenset({"C123"}), bindings={"U123": self.ctx})
        self.store = SQLiteStore(self.path)
        self.service = Service(self.store, [MockExecutor()], clock=lambda: self.now)
        self.api = FakeSlackAPI()
        self.adapter = SlackAdapter(self.service, self.config, self.api)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def body(self, *, event_id="Ev1", ts="1700000000.000001", **event):
        return {"type": "event_callback", "team_id": "T123", "api_app_id": "A123", "event_id": event_id,
                "event": {"type": "message", "user": "U123", "channel": "C123", "ts": ts,
                          "text": "!many review this", **event}}

    def reopen(self, *, executor=None, config=None):
        self.store.close()
        self.store = SQLiteStore(self.path)
        self.service = Service(self.store, [executor or MockExecutor()], clock=lambda: self.now)
        self.adapter = SlackAdapter(self.service, config or self.config, self.api)

    def assert_code(self, code, fn, *args):
        with self.assertRaises(HubError) as error:
            fn(*args)
        self.assertEqual(error.exception.code, code)

    def question(self):
        self.service.executors["mock"] = QuestionExecutor()
        result = self.adapter.receive_verified(self.body())
        self.service.run_once()
        self.adapter.deliver_once()
        return result

    def reply(self, **event):
        return self.body(event_id="Ev2", ts="1700000000.000003", thread_ts="1700000000.000001", text="42", **event)

    def test_request_result_same_thread_and_no_extra_dispatch(self):
        task = self.adapter.receive_verified(self.body())
        self.assertEqual(task["status"], "accepted")
        self.assertEqual(self.api.posts, [])
        self.assertTrue(self.service.run_once())
        delivery = self.adapter.deliver_once()
        self.assertEqual(delivery["status"], "sent")
        self.assertEqual(len(self.api.posts), 1)
        posted = self.api.posts[0]
        self.assertEqual(posted["channel"], "C123")
        self.assertEqual(posted["thread_ts"], "1700000000.000001")
        self.assertFalse(posted["mrkdwn"])
        self.assertFalse(posted["unfurl_links"])
        self.assertIn(task["task_id"], posted["text"])
        self.assertIsNone(self.adapter.deliver_once())
        self.assertFalse(self.service.run_once())
        self.assertEqual(self.service.task_get(self.ctx, task["task_id"])["deliveries"][0]["status"], "sent")

    def test_duplicate_event_and_message_id_only_one_task(self):
        first = self.adapter.receive_verified(self.body())
        self.assertEqual(first, self.adapter.receive_verified(self.body()))
        self.assertEqual(first, self.adapter.receive_verified(self.body(event_id="EvAlternate")))
        self.assertEqual(len(self.service.task_list(self.ctx)), 1)
        self.assert_code("conflict", self.adapter.receive_verified, self.body(text="!many altered"))
        self.assert_code("conflict", self.adapter.receive_verified, self.body(ts="1700000000.000009"))

    def test_replayed_message_cannot_borrow_another_existing_event_id(self):
        self.adapter.receive_verified(self.body())
        self.adapter.receive_verified(self.body(event_id="Ev2", ts="1700000000.000002"))
        self.assert_code("conflict", self.adapter.receive_verified, self.body(event_id="Ev2"))
        self.assertEqual(len(self.service.task_list(self.ctx)), 2)

    def test_alternate_event_alias_is_durable_and_cannot_be_reused(self):
        self.adapter.receive_verified(self.body())
        self.adapter.receive_verified(self.body(event_id="EvAlias"))
        self.reopen()
        self.assert_code("conflict", self.adapter.receive_verified,
                         self.body(event_id="EvAlias", ts="1700000000.000002"))
        self.assertEqual(len(self.service.task_list(self.ctx)), 1)

    def test_same_ids_different_workspace_or_app_are_rejected(self):
        self.adapter.receive_verified(self.body())
        for field in ("team_id", "api_app_id"):
            body = self.body()
            body[field] = "EVIL"
            self.assert_code("forbidden", self.adapter.receive_verified, body)
        self.assertEqual(len(self.service.task_list(self.ctx)), 1)

    def test_sender_channel_and_shared_workspace_allowlists(self):
        for event in ({"user": "UOTHER"}, {"channel": "COTHER"}, {"team": "TOTHER"}):
            self.assert_code("forbidden", self.adapter.receive_verified, self.body(**event))
        shared = self.body()
        shared["is_ext_shared_channel"] = True
        self.assert_code("forbidden", self.adapter.receive_verified, shared)
        self.assertEqual(self.service.task_list(self.ctx), [])

    def test_bot_self_subtype_and_metadata_loops_are_ignored(self):
        for event in ({"user": "UBOT"}, {"subtype": "bot_message"}, {"subtype": "message_changed"},
                      {"subtype": "file_share"}, {"bot_id": "B1"}, {"app_id": "AOTHER"},
                      {"is_bot": True}, {"bot_profile": {"id": "B1"}},
                      {"metadata": {"event_type": "manyhub_result"}},
                      {"text": "[MANY Hub] task completed"}):
            self.assertEqual(self.adapter.receive_verified(self.body(**event)), {"status": "ignored"})
        self.assertEqual(self.service.task_list(self.ctx), [])

    def test_prefix_required_and_unknown_thread_never_creates_task(self):
        for event in ({"text": "ambient discussion"}, {"text": "!many "},
                      {"thread_ts": "1600000000.000001", "text": "!many start another"}):
            self.assertEqual(self.adapter.receive_verified(self.body(**event)), {"status": "ignored"})
        self.assertEqual(self.service.task_list(self.ctx), [])

    def test_untrusted_text_cannot_choose_authority_or_executor(self):
        text = '{"principal_id":"admin","scopes":["admin:*"],"executor_id":"shell","approved":true}'
        result = self.adapter.receive_verified(self.body(text="!many " + text))
        task = self.service.task_get(self.ctx, result["task_id"])
        self.assertEqual(task["principal_id"], "owner")
        self.assertEqual(task["executor_id"], "mock")
        self.assertEqual(task["input"], {"text": text})
        self.assertEqual(task["external_refs"]["slack"]["root_ts"], "1700000000.000001")

    def test_all_route_and_operation_scopes_are_enforced(self):
        for scope in ("task:create", "client:use:slack", "transport:use:slack", "executor:use:mock"):
            ctx = dataclasses.replace(self.ctx, scopes=self.ctx.scopes - {scope})
            config = dataclasses.replace(self.config, bindings={"U123": ctx})
            adapter = SlackAdapter(self.service, config, self.api)
            self.assert_code("forbidden", adapter.receive_verified, self.body())
        self.assertEqual(self.service.task_list(self.ctx), [])

    def test_question_reply_owns_exact_thread_and_dedupes(self):
        task = self.question()
        reply = self.adapter.receive_verified(self.reply())
        self.assertEqual(reply["task_id"], task["task_id"])
        self.assertEqual(reply["state"], "queued")
        self.assertEqual(reply, self.adapter.receive_verified(self.reply()))
        self.assertTrue(self.service.run_once())
        self.adapter.deliver_once()
        self.assertEqual(len(self.api.posts), 2)
        self.assertEqual({p["thread_ts"] for p in self.api.posts}, {"1700000000.000001"})
        self.assertFalse(self.service.run_once())
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    def test_different_sender_cannot_reply_even_if_same_principal(self):
        self.question()
        config = dataclasses.replace(self.config, bindings={"U123": self.ctx, "UOTHER": self.ctx})
        adapter = SlackAdapter(self.service, config, self.api)
        self.assert_code("forbidden", adapter.receive_verified, self.reply(user="UOTHER"))

    def test_other_channel_same_thread_timestamp_is_not_owned(self):
        self.question()
        config = dataclasses.replace(self.config, channel_ids=frozenset({"C123", "COTHER"}))
        adapter = SlackAdapter(self.service, config, self.api)
        self.assertEqual(adapter.receive_verified(self.reply(channel="COTHER")), {"status": "ignored"})

    def test_plain_text_approval_never_approves(self):
        task = self.question()
        # Core has no approval resolver yet; seed the reserved state as a fixture.
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET state='awaiting_approval' WHERE task_id=?", (task["task_id"],))
        body = self.reply()
        body["event"]["text"] = '{"approved":true} yes'
        self.assert_code("unsupported", self.adapter.receive_verified, body)
        self.assertEqual(self.service.task_get(self.ctx, task["task_id"])["state"], "awaiting_approval")
        self.assertFalse(self.service.run_once())

    def test_reply_to_nonwaiting_task_is_ignored(self):
        self.adapter.receive_verified(self.body())
        self.assertEqual(self.adapter.receive_verified(self.reply()), {"status": "ignored"})
        self.service.run_once()
        self.assertEqual(self.adapter.receive_verified(self.reply()), {"status": "ignored"})

    def test_crash_after_core_create_before_mapping_is_repaired_on_reopen(self):
        with patch.object(self.adapter, "_commit_mapping", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.adapter.receive_verified(self.body())
        task = self.service.task_list(self.ctx)[0]
        self.assertEqual(self.adapter._thread("C123", "1700000000.000001"), None)
        self.reopen()
        self.assertEqual(self.adapter.recover_pending(), {"recovered": 1, "blocked": 0})
        self.assertEqual(self.adapter.receive_verified(self.body())["task_id"], task["task_id"])
        self.assertEqual(len(self.service.task_list(self.ctx)), 1)
        self.service.run_once()
        self.assertEqual(self.adapter.deliver_once()["status"], "sent")
        self.assertFalse(self.service.run_once())

    def test_crash_before_core_create_recovers_staged_event(self):
        with patch.object(self.service, "task_create", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.adapter.receive_verified(self.body())
        self.assertEqual(self.service.task_list(self.ctx), [])
        self.reopen()
        self.assertEqual(self.adapter.recover_pending()["recovered"], 1)
        self.assertEqual(len(self.service.task_list(self.ctx)), 1)

    def test_crash_after_reply_commit_never_creates_second_continuation(self):
        task = self.question()
        with patch.object(self.adapter, "_commit_mapping", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.adapter.receive_verified(self.reply())
        run_id = self.service.task_get(self.ctx, task["task_id"])["run_id"]
        self.reopen(executor=QuestionExecutor())
        self.assertEqual(self.adapter.recover_pending()["recovered"], 1)
        self.assertEqual(self.service.task_get(self.ctx, task["task_id"])["run_id"], run_id)
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    def test_delivery_repairs_crash_mapping_before_claim_even_without_event_retry(self):
        with patch.object(self.adapter, "_commit_mapping", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.adapter.receive_verified(self.body())
        self.service.run_once()
        self.reopen()
        self.assertEqual(self.adapter.deliver_once()["status"], "sent")
        self.assertEqual(len(self.api.posts), 1)

    def test_delayed_staged_reply_cannot_answer_a_later_question(self):
        class AlwaysQuestions(MockExecutor):
            def execute(self, request):
                return ExecutionResult("awaiting_input", {"question": "Next target?"})
        self.service.executors["mock"] = AlwaysQuestions()
        task = self.adapter.receive_verified(self.body())
        self.service.run_once()
        first_run = self.service.task_get(self.ctx, task["task_id"])["run_id"]
        self.adapter.deliver_once()
        with patch.object(self.service, "task_reply", side_effect=RuntimeError("crash before service commit")):
            with self.assertRaises(RuntimeError):
                self.adapter.receive_verified(self.reply())
        second = self.body(event_id="Ev3", ts="1700000000.000005", thread_ts="1700000000.000001", text="a different answer")
        self.adapter.receive_verified(second)
        self.service.run_once()
        later_run = self.service.task_get(self.ctx, task["task_id"])["run_id"]
        self.assertNotEqual(first_run, later_run)
        self.reopen(executor=AlwaysQuestions())
        self.assertEqual(self.adapter.recover_pending(), {"recovered": 0, "blocked": 1})
        self.assertEqual(self.service.task_get(self.ctx, task["task_id"])["run_id"], later_run)
        self.assertEqual(self.service.task_get(self.ctx, task["task_id"])["state"], "awaiting_input")
        self.assertFalse(self.service.run_once())
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)

    def test_permanently_blocked_inbox_does_not_starve_later_recovery(self):
        with patch.object(self.service, "task_create", side_effect=RuntimeError("crash")):
            for body in (self.body(ts="1600000000.000001"), self.body(event_id="Ev2")):
                with self.assertRaises(RuntimeError):
                    self.adapter.receive_verified(body)
        self.reopen()
        self.assertEqual(self.adapter.recover_pending(limit=1), {"recovered": 0, "blocked": 1})
        self.assertEqual(self.adapter.recover_pending(limit=1), {"recovered": 1, "blocked": 0})
        self.assertEqual(len(self.service.task_list(self.ctx)), 1)

    def test_two_concurrently_staged_replies_only_one_continuation_wins(self):
        task = self.question()
        bodies = [self.reply(), self.body(event_id="Ev4", ts="1700000000.000004", thread_ts="1700000000.000001", text="other answer")]
        barrier = threading.Barrier(2)
        errors = []
        def stage(body):
            try:
                barrier.wait()
                self.adapter.receive_verified(body)
            except RuntimeError:
                pass  # injected crash after staging, before Core call
            except Exception as error:
                errors.append(error)
        with patch.object(self.adapter, "_process", side_effect=RuntimeError("crash")):
            threads = [threading.Thread(target=stage, args=(body,)) for body in bodies]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.reopen(executor=QuestionExecutor())
        self.assertEqual(self.adapter.recover_pending(), {"recovered": 1, "blocked": 1})
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 2)
        self.assertEqual(self.service.task_get(self.ctx, task["task_id"])["state"], "queued")
        self.assertEqual(self.adapter.recover_pending(), {"recovered": 0, "blocked": 0})

    def test_first_seen_delayed_reply_cannot_answer_later_posted_question(self):
        class AlwaysQuestions(MockExecutor):
            def execute(self, request):
                return ExecutionResult("awaiting_input", {"question": "Next target?"})
        self.service.executors["mock"] = AlwaysQuestions()
        task = self.adapter.receive_verified(self.body())
        self.service.run_once()
        self.adapter.deliver_once()  # first question posted at .000002
        self.adapter.receive_verified(self.body(event_id="Ev5", ts="1700000000.000005",
            thread_ts="1700000000.000001", text="first answer"))
        self.service.run_once()
        self.api.response = {"ok": True, "channel": "C123", "ts": "1700000000.000006"}
        self.adapter.deliver_once()  # newer question at .000006
        current_run = self.service.task_get(self.ctx, task["task_id"])["run_id"]
        self.reopen(executor=AlwaysQuestions())
        old_reply = self.body(event_id="Ev4", ts="1700000000.000004", thread_ts="1700000000.000001", text="delayed old answer")
        self.assertEqual(self.adapter.receive_verified(old_reply), {"status": "ignored"})
        self.assertEqual(self.service.task_get(self.ctx, task["task_id"])["run_id"], current_run)
        self.assertFalse(self.service.run_once())
        new_reply = self.body(event_id="Ev7", ts="1700000000.000007", thread_ts="1700000000.000001", text="current answer")
        self.assertEqual(self.adapter.receive_verified(new_reply)["status"], "accepted")

    def test_reply_without_confirmed_question_delivery_is_ignored(self):
        self.service.executors["mock"] = QuestionExecutor()
        task = self.adapter.receive_verified(self.body())
        self.service.run_once()
        self.assertEqual(self.adapter.receive_verified(self.reply()), {"status": "ignored"})
        self.api.failure = True
        self.assertEqual(self.adapter.deliver_once()["status"], "uncertain")
        self.reopen(executor=QuestionExecutor())
        self.assertEqual(self.adapter.receive_verified(self.reply()), {"status": "ignored"})
        self.assertEqual(self.service.task_get(self.ctx, task["task_id"])["state"], "awaiting_input")
        self.assertFalse(self.service.run_once())

    def test_rebound_principal_cannot_adopt_pending_inbox(self):
        with patch.object(self.service, "task_create", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.adapter.receive_verified(self.body())
        config = dataclasses.replace(self.config, bindings={"U123": dataclasses.replace(self.ctx, principal_id="other")})
        self.reopen(config=config)
        self.assertEqual(self.adapter.recover_pending(), {"recovered": 0, "blocked": 1})
        self.assertEqual(self.service.task_list(self.ctx), [])

    def test_removed_channel_blocks_delivery(self):
        task = self.adapter.receive_verified(self.body())
        self.service.run_once()
        self.adapter = SlackAdapter(self.service, dataclasses.replace(self.config, channel_ids=frozenset({"COTHER"})), self.api)
        self.assertEqual(self.adapter.deliver_once()["status"], "uncertain")
        self.assertEqual(self.api.posts, [])
        self.assertEqual(self.service.task_get(self.ctx, task["task_id"])["state"], "succeeded")

    def test_lost_post_ack_is_uncertain_never_retried_or_leaked(self):
        task = self.adapter.receive_verified(self.body())
        self.service.run_once()
        self.api.failure = True
        result = self.adapter.deliver_once()
        self.assertEqual(result["status"], "uncertain")
        self.reopen()
        self.assertIsNone(self.adapter.deliver_once())
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(self.service.task_get(self.ctx, task["task_id"])["state"], "succeeded")
        with self.store.transaction() as db:
            text = "\n".join(db.iterdump())
        self.assertNotIn("SECRET_BODY", text)

    def test_crash_after_slack_post_before_core_ack_is_not_resent(self):
        self.adapter.receive_verified(self.body())
        self.service.run_once()
        with patch.object(self.service, "finish_delivery", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.adapter.deliver_once()
        self.reopen()
        self.now += 31
        self.assertIsNone(self.adapter.deliver_once())
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(self.service.task_list(self.ctx)[0]["deliveries"][0]["status"], "uncertain")

    def test_slack_rejection_or_mismatched_response_never_marks_sent(self):
        for response in ({"ok": False}, {"ok": True, "channel": "COTHER", "ts": "1700000001.000001"},
                         {"ok": True, "channel": "C123", "ts": "bad"}):
            suffix = str(len(self.service.task_list(self.ctx)) + 1)
            self.adapter.receive_verified(self.body(event_id="Ev" + suffix, ts="1700000000." + suffix.zfill(6)))
            self.service.run_once()
            self.api.response = response
            self.assertEqual(self.adapter.deliver_once()["status"], "uncertain")
        self.assertIsNone(self.adapter.deliver_once())

    def test_foreign_task_cannot_inject_slack_delivery_route(self):
        # Even a trusted other interface with route grants cannot cause a post
        # by putting arbitrary channel/thread IDs into generic task fields.
        self.service.task_create(self.ctx, {"request_id": "foreign", "client_id": "slack", "transport_id": "slack",
            "input": {}, "conversation_id": "T123:C123", "thread_id": "1700000000.000001"})
        self.service.run_once()
        self.assertEqual(self.adapter.deliver_once()["status"], "uncertain")
        self.assertEqual(self.api.posts, [])

    def test_reopen_preserves_exact_thread_and_fresh_adapter_dedupe(self):
        task = self.adapter.receive_verified(self.body())
        self.reopen()
        self.assertEqual(self.adapter.receive_verified(self.body())["task_id"], task["task_id"])
        self.service.run_once()
        self.adapter.deliver_once()
        self.assertEqual(len(self.api.posts), 1)

    def test_parallel_connections_dedupe_inbox_and_mapping(self):
        other_store = SQLiteStore(self.path)
        other = SlackAdapter(Service(other_store, [MockExecutor()], clock=lambda: self.now), self.config, self.api)
        results, errors = [], []
        barrier = threading.Barrier(2)
        def receive(adapter):
            try:
                barrier.wait()
                results.append(adapter.receive_verified(self.body())["task_id"])
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=receive, args=(adapter,)) for adapter in (self.adapter, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        other_store.close()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(results), 2)

    def test_output_is_bounded_no_mention_parsing_and_capabilities_are_honest(self):
        self.adapter.receive_verified(self.body(text="!many " + "x" * 4000 + " <@U123> <!channel>"))
        self.service.run_once()
        self.adapter.deliver_once()
        post = self.api.posts[0]
        self.assertLessEqual(len(post["text"]), 3500)
        self.assertEqual(post["parse"], "none")
        self.assertFalse(post["link_names"])
        self.assertFalse(self.adapter.capabilities()["interactive_approval"])
        self.assertFalse(self.adapter.capabilities()["live_verified"])

    def test_validation_and_frozen_allowlists(self):
        bindings = {"U123": self.ctx}
        config = dataclasses.replace(self.config, bindings=bindings)
        bindings["UEVIL"] = self.ctx
        self.assertNotIn("UEVIL", config.bindings)
        with self.assertRaises(ValueError):
            dataclasses.replace(self.config, channel_ids=frozenset())
        for event in ({"ts": "1.1"}, {"user": "U123\n"}, {"text": "!many " + "x" * 40000}):
            body = self.body()
            body["event"].update(event)
            with self.assertRaises(HubError):
                self.adapter.receive_verified(body)

    def test_socket_wrapper_connect_reconnect_close_is_offline(self):
        class Handler:
            def __init__(self): self.calls = []
            def connect(self): self.calls.append("connect")
            def disconnect(self): self.calls.append("disconnect")
            def close(self): self.calls.append("close")
        handler = Handler()
        runtime = SocketModeRuntime(self.adapter, handler)
        runtime.connect()
        runtime.reconnect()
        runtime.close()
        self.assertEqual(handler.calls, ["connect", "disconnect", "connect", "close"])
        self.assertEqual(self.api.posts, [])
        handler.connect = lambda: (_ for _ in ()).throw(RuntimeError("SECRET"))
        with self.assertRaises(HubError) as error:
            runtime.connect()
        self.assertNotIn("SECRET", str(error.exception))

    def test_stale_first_seen_message_is_not_executed(self):
        self.assert_code("expired", self.adapter.receive_verified,
                         self.body(ts="1600000000.000001"))
        self.assertEqual(self.service.task_list(self.ctx), [])
        self.assertFalse(self.service.run_once())

    def test_mentions_are_escaped_in_output(self):
        self.adapter.receive_verified(self.body(text="!many <@U123> <!channel> &"))
        self.service.run_once()
        self.adapter.deliver_once()
        text = self.api.posts[0]["text"]
        self.assertNotIn("<@U123>", text)
        self.assertIn("&lt;@U123&gt;", text)

    @unittest.skipUnless(importlib.util.find_spec("slack_bolt"), "optional Slack SDK is not installed")
    def test_official_sdk_factory_and_event_dispatch_without_network(self):
        from slack_bolt.request import BoltRequest
        from slack_sdk import WebClient
        from slack_sdk.web.slack_response import SlackResponse
        auth = SlackResponse(client=None, http_verb="POST", api_url="https://slack.invalid/api/auth.test", req_args={},
                             data={"ok": True, "team_id": "T123", "user_id": "UBOT", "bot_id": "B123"}, headers={}, status_code=200)
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")), \
                patch.object(WebClient, "auth_test", return_value=auth):
            runtime = SocketModeRuntime.from_tokens(self.adapter, bot_token="xoxb-OFFLINE-FIXTURE", app_token="xapp-OFFLINE-FIXTURE")
            try:
                self.assertEqual(runtime.adapter.client.retry_handlers, [])
                response = runtime.handler.app.dispatch(BoltRequest(body=self.body(), mode="socket_mode"))
                self.assertEqual(response.status, 200)
                self.assertEqual(len(self.service.task_list(self.ctx)), 1)
                replay = runtime.handler.app.dispatch(BoltRequest(body=self.body(), mode="socket_mode"))
                self.assertEqual(replay.status, 200)
                self.assertEqual(len(self.service.task_list(self.ctx)), 1)
                with patch.object(runtime.handler, "connect") as connect, patch.object(runtime.handler, "disconnect") as disconnect:
                    runtime.connect()
                    runtime.reconnect()
                    self.assertEqual(connect.call_count, 2)
                    self.assertEqual(disconnect.call_count, 1)
            finally:
                runtime.close()

    @unittest.skipUnless(importlib.util.find_spec("slack_bolt"), "optional Slack SDK is not installed")
    def test_official_sdk_factory_rejects_identity_and_hides_error_text(self):
        from slack_sdk import WebClient
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")):
            with patch.object(WebClient, "auth_test", return_value={"ok": True, "team_id": "TOTHER", "user_id": "UBOT"}):
                with self.assertRaises(HubError) as error:
                    SocketModeRuntime.from_tokens(self.adapter, bot_token="xoxb-OFFLINE-FIXTURE", app_token="xapp-OFFLINE-FIXTURE")
                self.assertEqual(error.exception.code, "forbidden")
            with patch.object(WebClient, "auth_test", side_effect=RuntimeError("SECRET_TOKEN_BODY")):
                with self.assertRaises(HubError) as error:
                    SocketModeRuntime.from_tokens(self.adapter, bot_token="xoxb-OFFLINE-FIXTURE", app_token="xapp-OFFLINE-FIXTURE")
                self.assertEqual(error.exception.code, "unavailable")
                self.assertNotIn("SECRET", str(error.exception))


if __name__ == "__main__":
    unittest.main()
