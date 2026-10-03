# MANY Hub protocol v0.1

The canonical API is `manyhub.service.Service`. CLI and the official MCP stdio
adapter call that API. MCP does not define the Core model. P1 opens no HTTP port,
creates no credentials, and has no remote MCP or OAuth flow.

## Authority

Every public Service method takes an immutable `Context(principal_id, profile_id,
scopes)` supplied by a trusted adapter. The default CLI and stdio processes bind
`Config.local_context()` once, under the invoking OS user's access to the local
database. MCP initialization metadata and tool arguments cannot select identity.

The local principal has task create/read/reply/cancel, executor list/use `mock`,
client use `local`, and transport use `local`. Grants and task ownership are both
checked by the Service. An unknown or other principal's task returns `not_found`.
An absent action, executor, client, or transport grant returns `forbidden`.
`admin:*` is not an ownership bypass. No CLI option adds grants or changes owner.

This is a local OS trust boundary, not network authentication or isolation from
other programs running as the same OS user. Use a private database directory and
one trusted profile. Never expose this process over a network stdio proxy. A
future network adapter must separately authenticate and bind each principal;
it must not use a default, generated, or shared universal credential.

## Common operations

| Service method | Required grant | Result |
| --- | --- | --- |
| `capabilities(context)` | authenticated adapter binding | Protocol, limits, operations, unsupported flags |
| `executor_list(context)` | `executor:list` | Only granted executors and verified capabilities |
| `task_create(context, request)` | `task:create` and executor/client/transport grants | Owned task snapshot |
| `task_get(context, task_id)` | `task:read` and ownership | Task snapshot |
| `task_list(context, limit=50)` | `task:read` | Owned snapshots, newest first; limit 1–100 |
| `task_reply(context, task_id, request)` | `task:reply`, executor grant, ownership | Task with explicit continuation run |
| `task_cancel(context, task_id)` | `task:cancel` and ownership | Updated task snapshot |

Mutation calls return a full snapshot only with `task:read`; without that grant,
they return only `task_id`, `request_id`, `run_id`, `state`, and `revision`.
Mutation permission never implicitly reveals existing input or results.

Worker claim/completion, delivery claim/completion, and recovery methods are
internal contracts. They are never published as MCP tools. `artifact.get` and
remote approval resolution are not implemented; capability discovery does not
advertise them.

### Create request

```json
{
  "request_id": "example-request-001",
  "executor_id": "mock",
  "client_id": "local",
  "transport_id": "local",
  "input": {"text": "hello"},
  "conversation_id": "conversation-001",
  "thread_id": "thread-001",
  "message_id": "message-001",
  "timeout_seconds": 30,
  "external_refs": {"example": {"work_item_id": "item-001"}}
}
```

`request_id` is required and should be generated once by the caller, then reused
for retries. `executor_id` defaults to `mock`; client/transport default to
`local`; input and external refs default to empty objects. The service also
accepts these optional fields:

- `expires_at`: absolute Unix seconds in the next 24 hours; default now + 1 hour.
- `source_event_id`: an adapter's stable inbound event ID.
- `origin_hub_id`: self-originating requests are rejected.
- `hop_count`: integer 0–3; default 0. Four or more hops are rejected.

Unknown top-level fields are rejected, including `principal_id`, `profile_id`,
`scopes`, and `approved`. Data nested in `input` remains inert data; it cannot
grant authority. Product-specific identifiers belong in adapter mappings or
`external_refs`, not required Core fields. Do not place tokens, passwords, or
other credentials in task data: normal task content is persisted and returned to
its authorized owner, and the Mock Executor intentionally echoes its input.

The encoded whole request and input are bounded to 32,768 UTF-8 bytes. Identifiers
are nonempty strings of at most 200 characters without control characters;
conversation/message/thread IDs may be empty. There are at most 16 reference
namespaces. Timeout is finite, greater than zero and at most 300 seconds. JSON
non-finite numbers and invalid Unicode are rejected. The CLI additionally rejects
duplicate JSON keys and bounds the raw input file before parsing it.

An exact replay under the same profile/principal/client/request ID returns the
original task. Reusing that ID with different content returns `conflict`.
Adapter event replay is scoped to profile/principal/transport/event ID. A matching
event with different content also conflicts. Never generate a new request ID to
work around an ambiguous outcome.

### Reply request

```json
{"request_id": "reply-001", "input": {"answer": "42"}}
```

Only `request_id` and `input` are accepted. A reply is an explicit continuation
of owned `awaiting_input` work when the executor advertises
`resume_owned_run=true`. A new `run_id` is issued under the same `task_id`.
Replaying the exact reply is idempotent. Replies do not resolve approvals;
`approved: true`, `y`, or claims of human permission are data only.

### Task snapshots and outcomes

Snapshots include `task_id`, caller `request_id`, current `run_id`, ownership,
client/transport/executor IDs, conversation/message/thread IDs, `created_at`,
`expires_at`, `revision`, state, input, result, external refs, and delivery status.
These identifiers are separate concepts and must not be substituted for one
another. Execution result is at most 65,536 encoded UTF-8 bytes.

Queue and delivery state are independent. CLI and MCP clients poll `task.get`;
reading a result is not an outbound delivery acknowledgement. The local route's
pending delivery record therefore need not become `sent` when the user reads it.
Only an authenticated delivery adapter records confirmed delivery.

Cancellation of queued work prevents dispatch and yields `cancelled`.
Cancellation of running work yields `cancel_requested`, which is not proof that
an external effect stopped. Unsupported executors cannot confirm cancellation.
Expired or lost claims become `result_uncertain` and are not automatically
re-executed; late results are recorded without reviving terminal work. An
operator must reconcile uncertain effects with the executor.

## CLI

Python 3.12+ is required. Core and CLI need only the standard library. Use the
installed `manyhub` entry point or equivalent `python -m manyhub`.

```sh
manyhub --database data/manyhub.sqlite3 capabilities --json
manyhub executor list --json
manyhub task create --input-file request.json --json
manyhub task get TASK_ID --json
manyhub task list --limit 50 --json
manyhub task reply TASK_ID --input-file reply.json --json
manyhub task cancel TASK_ID --json
manyhub worker --once --json
manyhub serve --local
```

All commands use the same database; its default is `data/manyhub.sqlite3` relative
to the process working directory. Use an explicit absolute `--database` path
when starting clients from different directories. `--database` and `--json` can
also follow the selected operation.

`task.create` and `task.reply` queue durable work. `worker --once` processes at
most one task and returns `{"processed": true}` or `{"processed": false}`.
`serve --local` and `worker` run the local queue continuously, with recovery on
each poll, and open no network listener. `--poll-interval` accepts 0.01–60 seconds
(default 0.25). Stop the foreground process with Ctrl-C. Work claimed by an
abruptly stopped process is reconciled conservatively after lease expiry.

Success goes to stdout. `--json` emits compact JSON; without it JSON is indented.
Errors go to stderr with no input, credential, raw exception, or path echo:

```json
{"error":{"code":"forbidden","message":"Required grant is not present"}}
```

Exit status is 0 for successful commands, 1 for operation failure, 2 for invalid
command arguments, and 130 for Ctrl-C. Missing MCP dependencies produce
`unavailable`; install the optional extra locally with `pip install '.[mcp]'`.
No dependency is downloaded automatically by the CLI.

## Official MCP stdio adapter

Install the project's pinned optional dependency, `mcp==1.29.0`. The adapter uses
the official SDK's FastMCP server and lifecycle, initialization, schema discovery,
JSON-RPC framing, calls, and notifications. It does not implement a partial
JSON-RPC server. This is the v1 SDK API, not an unpinned v2/main-branch API.

Launch with:

```sh
manyhub --database /absolute/private/path/hub.sqlite3 mcp --stdio
```

The `--stdio` flag is optional because stdio is the sole supported transport.
Run `manyhub --database /absolute/private/path/hub.sqlite3 serve --local` in a
separate process for execution. MCP only submits/reads tasks; it does not expose
worker or result-completion tools. Stdout is reserved entirely for MCP messages.
SDK diagnostics on stderr are redacted to prevent protocol-validation errors
from echoing untrusted payloads.

| MCP tool | Arguments |
| --- | --- |
| `manyhub_capabilities` | `{}` |
| `manyhub_executor_list` | `{}` |
| `manyhub_task_create` | `{"request": <create request>}` |
| `manyhub_task_get` | `{"task_id": "..."}` |
| `manyhub_task_list` | `{"limit": 50}` |
| `manyhub_task_reply` | `{"task_id": "...", "request": <reply request>}` |
| `manyhub_task_cancel` | `{"task_id": "..."}` |

Tool argument schemas forbid extra top-level properties and are validated before
SDK argument coercion. Type-mismatched limits, injected context/grants, and
unknown operations cannot broaden authority. Results provide the same JSON in
`structuredContent` and a text content block. Task results are task snapshots;
list results use `{"tasks": [...]}` or `{"executors": [...]}`. Errors set MCP
`isError=true` and contain the error envelope above. Protocol-level malformed
messages remain SDK-managed. Tool annotations are hints, never grants.

The integration test uses the official SDK `ClientSession` and `stdio_client`
against a real spawned process, including initialize, list tools, task calls,
invalid-input checks, and ping. No ChatGPT/Claude product integration, remote
MCP, OAuth setup, or live external service use is claimed by that test.

SDK reference: https://github.com/modelcontextprotocol/python-sdk/tree/v1.29.0

## Run-bound replies

A task.reply request may include expected_run_id. Core checks it atomically after
idempotency replay lookup, before state transition. A stale non-replay reply
conflicts rather than answering a different run. Slack requires this fence plus
a message timestamp after the current run's confirmed question post.

## Explicit local command configuration

CLI/MCP/worker processes can opt into a trusted fixed-command Executor using
--command-config /absolute/path/command.json. This path is an operator process
argument, never a tool or task argument. The file fixes executable, argv, working
directory and allowlist; no Hub environment is inherited. See adapters.md for
the POSIX-only trust contract. Without the flag, only Mock is registered.
