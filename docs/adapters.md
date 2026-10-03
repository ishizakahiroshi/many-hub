# Adapter contracts and verification

MANY Hub's Service is authoritative. SDKs translate authenticated transport input
into that API; they do not choose authority from chat text, change Core schema,
execute shell text, or resolve executor-owned approvals. Core imports neither
Slack nor MCP. The Slack SDK is optional and imported only by the live factory.

## Verification matrix (2026-10-03)

| Adapter / capability | Evidence | Live status |
| --- | --- | --- |
| Local CLI | CLI/interface tests | Local only |
| MCP v1 stdio | Official SDK plus interface tests; see compatibility document | Remote MCP disabled |
| Mock Executor | Core tests, no AI dependency | Effect-free fixture |
| Slack request → task → Mock → same-thread result | `tests/test_slack.py`, fake Slack API and real SQLite/Service | **Not live verified** |
| Slack sender/workspace/app/channel grants, dedupe, loops | Offline positive/negative fixtures | **Not live verified** |
| Slack mapping and inbox restart/crash repair | Fresh SQLite connections and injected crash boundaries | Offline verified |
| Slack question replies | Exact owned thread and `awaiting_input`; durable continuation dedupe | **Not live verified** |
| Slack connect/reconnect/close wrapper | Fake handler lifecycle plus official Bolt event dispatch with network blocked | Real connection/reconnection **unverified** |
| Slack interactive approval, files, bot-to-bot executors | Unsupported, capability false | Disabled |
| dot/Dots, Grok Bot, OpenClaw, Hermes, MANY-AI-CLI, Deskly | No authorized product round-trip | Unverified; not advertised as supported |

Flags in `SlackAdapter.capabilities()` describe the tested adapter behavior and
explicitly carry `verification=offline-fixtures`, `live_verified=false`, and
`socket_mode_live_verified=false`. They are not evidence that a workspace is
connected. No production Slack message, OAuth consent, token creation/entry,
permission/scope change, or deployment was performed for these fixtures.

## Slack P2

Implementation: `manyhub/adapters/slack.py`. This is a library integration, not a
Slack daemon or a new public CLI command. An operator-owned process composes the
adapter, Service worker, recovery, and delivery loop. It must keep running and
call `Service.run_once()` and `SlackAdapter.deliver_once()` to progress work.
`SocketModeRuntime.connect()` starts the SDK connection; it does not execute
tasks or drain the result outbox on its own.

### Trusted configuration

Construct `SlackConfig` with one exact `team_id`, `app_id`, `bot_user_id`, a
nonempty channel-ID allowlist, and an explicit user-ID → `Context` binding map.
Use stable Slack IDs, not display names. Each independent workspace/route needs
its own `transport_id`; do not reuse one transport ID for separate installations.
Use the least privilege grants needed by each binding:

- `task:create`, `task:read`, `task:reply` for the full round-trip
- `executor:use:<configured executor>`
- `client:use:<configured client>` and `transport:use:<configured transport>`

Configuration and bindings are trusted operator input. Never deserialize a
`Context`, executor ID, grants, route, or tokens from an incoming Slack message.
`examples/slack_config.example.json` is illustrative credential-free deployment
input, not an implemented config loader. No credential appears in the example.

`receive_verified(body)` is an **internal** authenticated-event boundary. Calling
it directly with arbitrary JSON does not authenticate a Slack user. Do not expose
it as an HTTP route. Use the official Socket Mode factory after separately
authorizing a real integration. The SDK validates the connection; the adapter
also checks the configured workspace/app and exact sender/channel allowlists.
Shared cross-workspace channels are intentionally rejected in this milestone.

### Inbound behavior

- An ordinary top-level human `message` beginning with `!many ` creates a task.
  The configurable prefix avoids treating ambient channel discussion as work.
- The original request is the one parent message. There is no additional parent
  post or acknowledgment post. Result/question/cancellation notifications use
  its exact `channel_id` and `thread_ts`.
- Only `message` events are consumed. `app_mention`, edits/deletions, all subtypes,
  bots, the app itself, and `manyhub_*` result metadata are ignored.
- The message body is one bounded `input.text` string. JSON-looking text stays
  text. Attachments, credentials, event envelope metadata, and authority fields
  are not forwarded to executors. Message creation time sets a one-hour expiry.
- Unknown thread replies are ignored, never treated as new task submissions.
  A known thread accepts only its original Slack user and matching configured
  principal/profile, with `task:reply` and `task:read` permissions. Every staged
  reply carries the captured `expected_run_id`; Core checks that fence atomically
  after request-ID deduplication. First-seen replies must also have a Slack
  timestamp later than the confirmed question post for that same run. Delayed
  old replies cannot answer a later question. If a question post is uncertain,
  thread replies remain closed pending operator reconciliation.
- Plain text, including `y`, `approved`, or `{"approved":true}`, cannot resolve
  an approval. No block-action approval listener exists.

### Durability and failure boundaries

The adapter owns the `manyhub_slack_inbox`, `manyhub_slack_threads`,
`manyhub_slack_events` (event aliases), and `manyhub_slack_questions` (run/post
correlation) SQLite tables.
Slack-specific IDs do not become Core schema requirements. The create request
also records a minimal `external_refs.slack` mapping atomically with the Core task.
Backup/restore of the database includes the adapter tables.

1. Validate the authenticated event and derive its stable message identity.
2. Persist a normalized inbox record before calling Service.
3. Call `task_create` or `task_reply` using a deterministic request ID.
4. Persist the thread mapping and mark inbox processing complete.

A crash between steps 2–4 is repaired by `recover_pending()`, including after
reopening the database with a new adapter. Core idempotency returns the existing
task/continuation; no executor dispatch is replayed by the adapter. Recovery runs
on connect and before delivery and should additionally run periodically. Revoked
bindings are blocked, never rebound to a different principal during recovery.
Permanent failures leave the pending recovery queue so they cannot starve later
records. An explicit matching event retry can be considered after an operator
restores a binding. Rate-limited records remain pending.
Duplicate event IDs with different content are rejected; duplicate message IDs
return the same task even when the delivery envelope changes.

Outbound work is claimed through `Service.claim_delivery(configured_transport)`.
A post destination must match an adapter-owned task/thread mapping, current
allowlist, and current principal binding. Generic task fields alone cannot direct
an arbitrary Slack post. Output is short plain text with mention/link parsing and
unfurls disabled, escaped special characters, and a task ID for full-result lookup.
The adapter does not upload files or split one result into many posts.

**No exactly-once external delivery guarantee is made.** A timeout, invalid
response, API error, or lost acknowledgement becomes `uncertain`. A process crash
after claiming/sending becomes uncertain when the Core lease expires. No automatic
resend follows, even across reconnect/restart. A definitive API rejection is also
conservatively uncertain under the current Core contract. Inspect and reconcile
Slack history before any separately authorized manual recovery. Task success and
message delivery success remain different statuses.

### Optional official SDK

The candidate is official [`slack-bolt==1.30.0`](https://pypi.org/project/slack-bolt/)
(MIT, published 2026-07-15), using its synchronous built-in `SocketModeHandler`
and the official `slack_sdk.WebClient`. The factory lazily imports it. Offline SDK integration tests use Bolt 1.30.0 and
Slack SDK 3.45.0 with `auth.test` fixture responses and network connections blocked.
The Core
and fixture suite do not need either Slack package. Before packaging a release,
pin and inventory transitive dependencies and licenses as well.

`SocketModeRuntime.from_tokens(...)` is the explicitly live construction step:
it checks `auth.test` against configured workspace/bot identity, keeps Bolt's
normal verification/self-event protections, and uses `process_before_response`
so listener work reaches the durable inbox before acknowledgment. It must never
be called with production credentials as part of a unit test. The factory takes
explicit secrets; it never creates tokens, performs OAuth, saves credentials,
or forwards them to Service/executors. Supply secrets only via an authorized
operator-controlled mechanism outside request/config JSON.

The Web API client has `retry_handlers=[]` to avoid a hidden post retry after an
ambiguous network failure, with a bounded request timeout. Socket reconnection is
handled separately by official SDK `auto_reconnect_enabled=True`; manual wrapper
`reconnect()` also disconnects and connects without resending results. Trace
logging is disabled; a non-propagating private logger suppresses SDK payloads and
exception detail. Do not turn on SDK/wire logging with real credentials.

Slack setup, installation, scopes, and real identity/permission tests remain an
explicitly authorized operator step. An app-level Socket Mode token requires
`connections:write`; message subscription and bot scopes depend on the selected
conversation types. Posting needs `chat:write`. This document is not permission
to create those tokens, add scopes, install an app, or post in a workspace.

### Official references

- [Bolt Socket Mode](https://docs.slack.dev/tools/bolt-python/concepts/socket-mode/)
- [Built-in handler options](https://docs.slack.dev/tools/bolt-python/reference/adapter/socket_mode/builtin/index.html)
- [Bolt app verification and process-before-response options](https://docs.slack.dev/tools/bolt-python/reference/app/app.html)
- [Event listener acknowledgment timing](https://docs.slack.dev/tools/bolt-python/reference/listener/thread_runner.html)
- [Slack WebClient and retry handlers](https://docs.slack.dev/tools/python-slack-sdk/reference/web/client.html)
- [Events API event envelope and retries](https://docs.slack.dev/apis/events-api/)
- [chat.postMessage threading and formatting](https://docs.slack.dev/reference/methods/chat.postMessage/)

Read 2026-10-03. These are interface references, not live workspace test evidence.

## Generic fixed-command Executor P3

`manyhub.executors.command.CommandExecutor` is an operator-composed, POSIX-only
adapter. The local real-process example is `python examples/command_roundtrip.py`.
It executes the project's effect-free `structured_worker.py` using a fixed
absolute Python interpreter, fixed `-I` argv, a dedicated temporary working
directory, JSON stdin and a structured stdout result. The complete Service →
real subprocess → Service → local delivery round-trip is tested.

An administrator supplies `CommandSpec`: executor ID, absolute executable,
explicit executable allowlist, fixed argument tuple, fixed workspace, timeout
and output cap. Task data cannot set these. No shell or inherited Hub environment
is used; stdout plus stderr is bounded, raw stderr is discarded, and process
setup failure/timeout/invalid output yields a conservative uncertain result.
No universal external-effect idempotency is promised and uncertainty is never
retried automatically. Nonzero exit reports process failure, not effect rollback.

The executable, interpreter scripts, imported libraries, workspace and writable
parent paths must all be trusted and protected from untrusted modification.
The executable allowlist validates a path; it is not a content-signing system.
POSIX process-group cleanup is **not a complete process-tree sandbox**. Workers
must not daemonize, call `setsid`, escape their process group, or launch persistent
background work. A hostile program can escape this contract. Use OS/container
isolation and a separate security review before more powerful executors.

Capabilities: start_run and structured_result only. No cancel confirmation,
resume_owned_run, interactive approval, artifact publishing, bot login or
external AI account is advertised. The adapter is not automatically enabled by
CLI/MCP/Compose. Windows is disabled pending equivalent process-tree lifecycle
support; Linux is tested locally and other POSIX platforms require CI evidence.

MCP mutation annotations are conservative for embedding with effectful executors:
create/reply/cancel are non-read-only, potentially destructive/open-world. The
hints do not replace grants or executor-owned approval.

To explicitly enable a trusted command for a local CLI/MCP/worker process, pass
`--command-config /absolute/path/command.json` each time. Without that flag only
Mock is registered and `executor:use:<configured-id>` is absent. The file must
contain `executor_id`, absolute `executable`, a fixed `arguments` array, absolute
`workspace`, and an `allowed_executables` array. Optional `timeout_seconds` and
`max_output_bytes` can only lower the bounded runtime envelope. There is no
environment override, shell mode or task-supplied configuration path. Treat this
operator file as executable policy and protect it accordingly.

The test `test_cli_to_real_executor_to_cli_full_roundtrip` launches separate CLI
processes for create, worker and get, uses the trusted project JSON worker and
checks the structured result. It also proves that creation is denied without the
explicit command configuration/grant. This tests local real execution, not an
external AI account or production service.
