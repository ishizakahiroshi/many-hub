"""Single-workspace Slack transport; importing this module has no network effects.

Only feed receive_verified() events authenticated by the official Socket Mode
connection. It is an internal boundary, NOT an HTTP handler or authentication
mechanism. All authority and routes come from operator configuration. No token,
raw SDK exception, or unfiltered event envelope is persisted here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from ..model import Context, HubError
from ..service import Service, bounded_json, identifier
from ..store import encode

BOLT_VERSION = "1.30.0"
_TS = re.compile(r"[0-9]{1,20}\.[0-9]{6}\Z")
_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_MARKER = "[MANY Hub]"


def _id(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise HubError("invalid_request", "Invalid Slack identifier")
    return value


def _ts(value: Any) -> str:
    if not isinstance(value, str) or not _TS.fullmatch(value):
        raise HubError("invalid_request", "Invalid Slack timestamp")
    return value


def _hash(value: Any) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


@dataclass(frozen=True)
class SlackConfig:
    """Trusted, credential-free configuration; bindings are never read from chat."""

    team_id: str
    app_id: str
    bot_user_id: str
    channel_ids: frozenset[str]
    bindings: Mapping[str, Context]
    transport_id: str = "slack"
    client_id: str = "slack"
    executor_id: str = "mock"
    command_prefix: str = "!many "

    def __post_init__(self) -> None:
        for value in (self.team_id, self.app_id, self.bot_user_id):
            _id(value)
        for value in (self.transport_id, self.client_id, self.executor_id):
            identifier(value, "configured route")
        if not self.channel_ids or not self.bindings:
            raise ValueError("Slack allowlists must not be empty")
        for value in self.channel_ids:
            _id(value)
        for user_id, context in self.bindings.items():
            _id(user_id)
            if user_id == self.bot_user_id or not isinstance(context, Context):
                raise ValueError("Invalid Slack principal binding")
        if not isinstance(self.command_prefix, str) or not self.command_prefix.strip() or len(self.command_prefix) > 40:
            raise ValueError("An explicit Slack command prefix is required")
        object.__setattr__(self, "channel_ids", frozenset(self.channel_ids))
        object.__setattr__(self, "bindings", MappingProxyType(dict(self.bindings)))


class SlackWebAPI(Protocol):
    def chat_postMessage(self, **kwargs: Any) -> Any: ...


class SlackAdapter:
    """Durable receive/return bridge, usable entirely with an offline fake API.

    Adapter-owned inbox writes precede service calls. Core request idempotency
    repairs a crash after task creation/continuation and before mapping commit.
    Run recover_pending() at startup and periodically; it performs no Slack API
    requests and never reruns an executor itself.
    """

    def __init__(self, service: Service, config: SlackConfig, client: SlackWebAPI | None = None):
        self.service, self.config, self.client = service, config, client
        with service.store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS manyhub_slack_inbox(
                record_id TEXT PRIMARY KEY, transport_id TEXT NOT NULL, team_id TEXT NOT NULL,
                event_id TEXT NOT NULL, channel_id TEXT NOT NULL, user_id TEXT NOT NULL,
                message_ts TEXT NOT NULL, root_ts TEXT NOT NULL, profile_id TEXT NOT NULL,
                principal_id TEXT NOT NULL, mode TEXT NOT NULL, request_json TEXT NOT NULL,
                content_hash TEXT NOT NULL, status TEXT NOT NULL, task_id TEXT,
                UNIQUE(transport_id,team_id,event_id),
                UNIQUE(transport_id,team_id,channel_id,message_ts))""")
            db.execute("""CREATE TABLE IF NOT EXISTS manyhub_slack_threads(
                transport_id TEXT NOT NULL, team_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                root_ts TEXT NOT NULL, user_id TEXT NOT NULL, profile_id TEXT NOT NULL,
                principal_id TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE,
                PRIMARY KEY(transport_id,team_id,channel_id,root_ts))""")
            db.execute("""CREATE TABLE IF NOT EXISTS manyhub_slack_events(
                transport_id TEXT NOT NULL, team_id TEXT NOT NULL, event_id TEXT NOT NULL,
                record_id TEXT NOT NULL REFERENCES manyhub_slack_inbox(record_id),
                PRIMARY KEY(transport_id,team_id,event_id))""")
            db.execute("""INSERT OR IGNORE INTO manyhub_slack_events
                SELECT transport_id,team_id,event_id,record_id FROM manyhub_slack_inbox""")
            db.execute("""CREATE TABLE IF NOT EXISTS manyhub_slack_questions(
                task_id TEXT NOT NULL, run_id TEXT NOT NULL, posted_ts TEXT NOT NULL,
                PRIMARY KEY(task_id,run_id))""")

    def capabilities(self) -> dict[str, Any]:
        return {"adapter": "slack", "verification": "offline-fixtures",
                "live_verified": False, "socket_mode_live_verified": False,
                "receive": True, "threaded_results": True, "durable_dedupe": True,
                "question_reply": True, "interactive_approval": False,
                "bot_executor": False, "file_upload": False,
                "delivery_guarantee": "durable intent; uncertain sends require reconciliation"}

    def _context(self, user_id: str, *, channel_id: str) -> Context:
        if channel_id not in self.config.channel_ids or user_id not in self.config.bindings:
            raise HubError("forbidden", "Slack sender or channel is not allowed")
        context = self.config.bindings[user_id]
        context.require(f"client:use:{self.config.client_id}")
        context.require(f"transport:use:{self.config.transport_id}")
        context.require(f"executor:use:{self.config.executor_id}")
        return context

    def _thread(self, channel: str, root: str) -> dict[str, Any] | None:
        with self.service.store.transaction() as db:
            row = db.execute("SELECT * FROM manyhub_slack_threads WHERE transport_id=? AND team_id=? AND channel_id=? AND root_ts=?",
                             (self.config.transport_id, self.config.team_id, channel, root)).fetchone()
            return dict(row) if row is not None else None

    def receive_verified(self, body: dict[str, Any]) -> dict[str, Any]:
        """Accept an authenticated Events API body. Returns local status, never posts.

        A root human message must begin with command_prefix. Replies continue
        only a known thread owned by the same configured user/principal/profile.
        A durable retry is permitted after state advancement solely so Core can
        return its existing idempotent acknowledgement.
        """
        if not isinstance(body, dict):
            raise HubError("invalid_request", "Expected Slack event object")
        if body.get("type") != "event_callback":
            return {"status": "ignored"}
        if body.get("team_id") != self.config.team_id or body.get("api_app_id") != self.config.app_id:
            raise HubError("forbidden", "Slack workspace or app is not allowed")
        event = body.get("event")
        if not isinstance(event, dict) or event.get("type") != "message":
            return {"status": "ignored"}
        if (event.get("subtype") is not None or event.get("bot_id") or event.get("app_id")
                or event.get("bot_profile") or event.get("is_bot")
                or event.get("user") == self.config.bot_user_id):
            return {"status": "ignored"}
        # Cross-workspace shared channels and forwarded team identities are not
        # supported by the single-workspace identity binding in this milestone.
        if body.get("is_ext_shared_channel") or event.get("team", self.config.team_id) != self.config.team_id:
            raise HubError("forbidden", "Cross-workspace events are not supported")
        metadata = event.get("metadata")
        if isinstance(metadata, dict) and str(metadata.get("event_type", "")).startswith("manyhub_"):
            return {"status": "ignored"}
        channel, user = _id(event.get("channel")), _id(event.get("user"))
        context = self._context(user, channel_id=channel)
        event_id, message_ts = _id(body.get("event_id")), _ts(event.get("ts"))
        root = _ts(event.get("thread_ts", message_ts))
        text = event.get("text")
        if not isinstance(text, str) or not text.strip() or text.startswith(_MARKER):
            return {"status": "ignored"}
        bounded_json({"text": text})
        mode = "create" if root == message_ts else "reply"
        if mode == "create":
            if not text.startswith(self.config.command_prefix):
                return {"status": "ignored"}
            text = text[len(self.config.command_prefix):].strip()
            if not text:
                return {"status": "ignored"}
            context.require("task:create")
        else:
            context.require("task:reply")
            context.require("task:read")
        # Stable message key also handles the same message delivered with a new
        # envelope/event id. Identity and content changes are rejected below.
        key = _hash([self.config.transport_id, self.config.team_id, channel, message_ts])
        normalized = {"team_id": self.config.team_id, "channel_id": channel,
                      "root_ts": root, "message_ts": message_ts, "user_id": user,
                      "profile_id": context.profile_id, "principal_id": context.principal_id,
                      "client_id": self.config.client_id, "executor_id": self.config.executor_id,
                      "mode": mode, "text": text}
        fingerprint = _hash(normalized)
        with self.service.store.transaction() as db:
            alias = db.execute("SELECT record_id FROM manyhub_slack_events WHERE transport_id=? AND team_id=? AND event_id=?",
                               (self.config.transport_id, self.config.team_id, event_id)).fetchone()
            if alias is not None and alias[0] != key:
                raise HubError("conflict", "Slack event identity was reused")
            prior = db.execute("SELECT * FROM manyhub_slack_inbox WHERE record_id=?", (key,)).fetchone()
            if prior is not None:
                if prior["content_hash"] != fingerprint:
                    raise HubError("conflict", "Slack message identity was reused")
                self._bind_event(db, event_id, key)
        if prior is not None:
            return self._process(key)
        task_id = None
        if mode == "reply":
            thread = self._thread(channel, root)
            if thread is None:
                return {"status": "ignored"}
            if (thread["user_id"], thread["profile_id"], thread["principal_id"]) != (user, context.profile_id, context.principal_id):
                raise HubError("forbidden", "Slack thread is not owned by this sender")
            task_id = thread["task_id"]
            task = self.service.task_get(context, task_id)
            if task["state"] == "awaiting_approval":
                raise HubError("unsupported", "Text cannot resolve executor approval")
            if task["state"] != "awaiting_input":
                return {"status": "ignored"}
            with self.service.store.transaction() as db:
                question = db.execute("SELECT posted_ts FROM manyhub_slack_questions WHERE task_id=? AND run_id=?",
                                      (task_id, task["run_id"])).fetchone()
            # A first-seen old Slack event must not answer a newer question. If
            # the question send itself is uncertain, correlation stays closed.
            if question is None or tuple(map(int, message_ts.split("."))) <= tuple(map(int, question[0].split("."))):
                return {"status": "ignored"}
            request = {"request_id": "slack-reply:" + key, "expected_run_id": task["run_id"], "input": {"text": text}}
        else:
            request = {"request_id": "slack:" + key, "source_event_id": "slack:" + key,
                       "client_id": self.config.client_id, "transport_id": self.config.transport_id,
                       "executor_id": self.config.executor_id, "input": {"text": text},
                       "expires_at": int(message_ts.split(".", 1)[0]) + 3600,
                       "conversation_id": self.config.team_id + ":" + channel,
                       "message_id": message_ts, "thread_id": root,
                       "external_refs": {"slack": {"team_id": self.config.team_id,
                            "channel_id": channel, "root_ts": root, "user_id": user}}}
        bounded_json(request)
        with self.service.store.transaction() as db:
            db.execute("""INSERT OR IGNORE INTO manyhub_slack_inbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (key, self.config.transport_id, self.config.team_id, event_id, channel, user,
                        message_ts, root, context.profile_id, context.principal_id, mode,
                        encode(request), fingerprint, "pending", task_id))
            row = db.execute("SELECT * FROM manyhub_slack_inbox WHERE record_id=?", (key,)).fetchone()
            if row is None or row["content_hash"] != fingerprint:
                raise HubError("conflict", "Slack event identity was reused")
            self._bind_event(db, event_id, key)
        return self._process(key)

    def _bind_event(self, db: Any, event_id: str, record_id: str) -> None:
        db.execute("INSERT OR IGNORE INTO manyhub_slack_events VALUES(?,?,?,?)",
                   (self.config.transport_id, self.config.team_id, event_id, record_id))
        alias = db.execute("SELECT record_id FROM manyhub_slack_events WHERE transport_id=? AND team_id=? AND event_id=?",
                           (self.config.transport_id, self.config.team_id, event_id)).fetchone()
        if alias[0] != record_id:
            raise HubError("conflict", "Slack event identity was reused")

    def _process(self, record_id: str) -> dict[str, Any]:
        with self.service.store.transaction() as db:
            row = db.execute("SELECT * FROM manyhub_slack_inbox WHERE record_id=? AND transport_id=? AND team_id=?",
                             (record_id, self.config.transport_id, self.config.team_id)).fetchone()
        if row is None:
            raise HubError("not_found", "Slack event not found")
        context = self._context(row["user_id"], channel_id=row["channel_id"])
        if (context.profile_id, context.principal_id) != (row["profile_id"], row["principal_id"]):
            raise HubError("forbidden", "Slack principal binding changed")
        request = json.loads(row["request_json"])
        if row["mode"] == "create":
            if (request["client_id"], request["executor_id"]) != (self.config.client_id, self.config.executor_id):
                raise HubError("forbidden", "Slack route binding changed")
            task = self.service.task_create(context, request)
        else:
            thread = self._thread(row["channel_id"], row["root_ts"])
            if thread is None or thread["task_id"] != row["task_id"]:
                raise HubError("forbidden", "Slack reply mapping changed")
            task = self.service.task_reply(context, row["task_id"], request)
        self._commit_mapping(row, task["task_id"])
        return {"status": "accepted", "task_id": task["task_id"], "state": task["state"]}

    def _commit_mapping(self, row: Any, task_id: str) -> None:
        """Separate crash boundary, repaired by replaying the durable inbox."""
        with self.service.store.transaction() as db:
            if row["mode"] == "create":
                db.execute("INSERT OR IGNORE INTO manyhub_slack_threads VALUES(?,?,?,?,?,?,?,?)",
                           (row["transport_id"], row["team_id"], row["channel_id"], row["root_ts"],
                            row["user_id"], row["profile_id"], row["principal_id"], task_id))
                existing = db.execute("SELECT task_id FROM manyhub_slack_threads WHERE transport_id=? AND team_id=? AND channel_id=? AND root_ts=?",
                                      (row["transport_id"], row["team_id"], row["channel_id"], row["root_ts"])).fetchone()
                if existing is None or existing[0] != task_id:
                    raise HubError("conflict", "Slack thread already belongs to another task")
            db.execute("UPDATE manyhub_slack_inbox SET task_id=?,status='done' WHERE record_id=?", (task_id, row["record_id"]))

    def recover_pending(self, limit: int = 100) -> dict[str, int]:
        """Retry only deterministic service operations, never an external send.

        Revoked bindings remain blocked; reconfiguration cannot silently reassign
        their work. Status contains counts only, not event data or error text.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Recovery limit must be between 1 and 1000")
        with self.service.store.transaction() as db:
            rows = db.execute("SELECT record_id FROM manyhub_slack_inbox WHERE transport_id=? AND team_id=? AND status='pending' ORDER BY rowid LIMIT ?",
                              (self.config.transport_id, self.config.team_id, limit)).fetchall()
        recovered = blocked = 0
        for row in rows:
            try:
                self._process(row[0])
                recovered += 1
            except HubError as error:
                if error.code != "rate_limited":
                    # Permanent failures must not starve later crash repair.
                    # An explicit matching event retry can still be considered
                    # after an operator restores a revoked binding.
                    with self.service.store.transaction() as db:
                        db.execute("UPDATE manyhub_slack_inbox SET status='blocked' WHERE record_id=? AND status='pending'", (row[0],))
                blocked += 1
        return {"recovered": recovered, "blocked": blocked}

    def deliver_once(self) -> dict[str, Any] | None:
        """Post at most one claimed delivery; never automatically retry a send.

        Network errors, bad responses, and lost acknowledgements are uncertain.
        Even a definitive Slack rejection is conservatively held for operator
        reconciliation by the current Core delivery contract.
        """
        if self.client is None:
            raise HubError("unavailable", "Slack API client is not configured")
        self.recover_pending()
        delivery = self.service.claim_delivery(self.config.transport_id)
        if delivery is None:
            return None
        payload = delivery["payload"]
        delivered = False
        try:
            with self.service.store.transaction() as db:
                row = db.execute("SELECT * FROM manyhub_slack_threads WHERE task_id=? AND transport_id=? AND team_id=?",
                                 (payload["task_id"], self.config.transport_id, self.config.team_id)).fetchone()
            if row is None:
                raise HubError("forbidden", "No authorized Slack thread mapping")
            context = self._context(row["user_id"], channel_id=row["channel_id"])
            if (context.profile_id, context.principal_id) != (row["profile_id"], row["principal_id"]):
                raise HubError("forbidden", "Slack principal binding changed")
            task = self.service.task_get(context, row["task_id"])
            if (task["transport_id"] != self.config.transport_id or task["client_id"] != self.config.client_id
                    or task["executor_id"] != self.config.executor_id
                    or payload["thread_id"] != row["root_ts"]
                    or payload["conversation_id"] != self.config.team_id + ":" + row["channel_id"]):
                raise HubError("forbidden", "Slack delivery route does not match")
            text = f"{_MARKER} {payload['task_id']} · {payload['state']}\n{encode(payload['output'])}"
            text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if len(text) > 3500:
                text = text[:3400] + "\n[Result shortened. Retrieve the full result by task_id.]"
            response = self.client.chat_postMessage(
                channel=row["channel_id"], thread_ts=row["root_ts"], text=text,
                mrkdwn=False, parse="none", link_names=False, unfurl_links=False,
                unfurl_media=False, reply_broadcast=False,
                metadata={"event_type": "manyhub_result", "event_payload": {
                    "hub_instance_id": self.service.hub_instance_id,
                    "task_id": payload["task_id"], "outbox_id": delivery["outbox_id"]}})
            delivered = (response.get("ok") is True and response.get("channel") == row["channel_id"]
                         and isinstance(response.get("ts"), str) and bool(_TS.fullmatch(response["ts"])))
            if delivered and payload["state"] == "awaiting_input":
                with self.service.store.transaction() as db:
                    db.execute("INSERT OR IGNORE INTO manyhub_slack_questions VALUES(?,?,?)",
                               (payload["task_id"], payload["run_id"], response["ts"]))
        except Exception:
            # SlackApiError / network exception strings can contain credentials,
            # URLs, request bodies, or response data. Never log or persist them.
            delivered = False
        self.service.finish_delivery(self.config.transport_id, delivery["outbox_id"],
                                     delivery["claim_id"], delivered=delivered)
        return {"outbox_id": delivery["outbox_id"], "status": "sent" if delivered else "uncertain"}


class SocketModeRuntime:
    """Lifecycle wrapper. SDK performs reconnects; pending sends are not replayed.

    Construction of the real SDK objects happens only in from_tokens(), which
    performs auth.test and must only be used after live access is authorized.
    Direct construction with a fake handler is fully offline.
    """

    def __init__(self, adapter: SlackAdapter, handler: Any):
        self.adapter, self.handler = adapter, handler

    @classmethod
    def from_tokens(cls, adapter: SlackAdapter, *, bot_token: str, app_token: str) -> SocketModeRuntime:
        try:
            from slack_bolt import App
            from slack_bolt.adapter.socket_mode import SocketModeHandler
            from slack_sdk import WebClient
        except ImportError:
            raise HubError("unavailable", "Install the optional pinned Slack SDK") from None
        # Dedicated non-propagating logger avoids SDK debug/event/exception data.
        logger = logging.getLogger("manyhub.slack.private")
        logger.setLevel(logging.CRITICAL + 1)
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        if not isinstance(bot_token, str) or not bot_token.startswith("xoxb-") or not isinstance(app_token, str) or not app_token.startswith("xapp-"):
            raise HubError("invalid_request", "Explicit bot and app tokens are required")
        if "SLACK_CLIENT_ID" in os.environ and "SLACK_CLIENT_SECRET" in os.environ:
            raise HubError("invalid_request", "Implicit OAuth environment configuration is not supported")
        try:
            client = WebClient(token=bot_token, timeout=10, retry_handlers=[], logger=logger)
            identity = client.auth_test()
            if identity.get("ok") is not True or identity.get("team_id") != adapter.config.team_id or identity.get("user_id") != adapter.config.bot_user_id:
                raise HubError("forbidden", "Slack bot identity does not match configuration")
            app = App(client=client, token=bot_token, signing_secret="", logger=logger, process_before_response=True)
            @app.event("message")
            def on_message(body: dict[str, Any]) -> None:
                # No say(): every result must go through the durable outbox.
                try:
                    adapter.receive_verified(body)
                except HubError as error:
                    if error.code not in {"invalid_request", "forbidden", "conflict", "unsupported", "limit_exceeded"}:
                        raise HubError("unavailable", "Slack event processing deferred") from None
            @app.error
            def on_error(error: Exception) -> None:
                # Deliberately do not emit exception text or the event body.
                pass
            handler = SocketModeHandler(app, app_token=app_token, web_client=client, logger=logger,
                                        auto_reconnect_enabled=True, trace_enabled=False,
                                        all_message_trace_enabled=False, ping_pong_trace_enabled=False)
            adapter.client = client
            return cls(adapter, handler)
        except HubError:
            raise
        except Exception:
            raise HubError("unavailable", "Slack SDK initialization failed") from None

    def connect(self) -> None:
        self.adapter.recover_pending()
        try:
            self.handler.connect()
        except Exception:
            raise HubError("unavailable", "Slack connection failed") from None

    def reconnect(self) -> None:
        try:
            self.handler.disconnect()
        except Exception:
            raise HubError("unavailable", "Slack disconnect failed") from None
        self.connect()

    def close(self) -> None:
        try:
            self.handler.close()
        except Exception:
            raise HubError("unavailable", "Slack close failed") from None
