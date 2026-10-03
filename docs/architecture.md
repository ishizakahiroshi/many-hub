# Architecture

## Layers

Client → authenticated Transport/Client Adapter → Service → Store/outbox →
Executor Adapter → fenced result → Service/events/delivery outbox → Transport.

The Service API is authoritative. CLI, MCP and HTTP are interchangeable entry
points. The Core neither imports a product SDK nor branches on a product name.
Task IDs are distinct from caller request IDs and attempt/continuation run IDs.
Product-specific identifiers live in optional opaque `external_refs` or an
adapter-owned mapping. No Slack/MANY/Deskly ID is required by the schema.

The initial process is a single hub with synchronous, bounded executor adapters.
SQLite permits multiple connections to compete for a claim, but this is not a
multi-node deployment. Runtime SDKs are optional dependencies outside Core.

## Durability boundary

Task creation atomically records its owner, request fingerprint, optional inbox
receipt, first run, creation event, external references and execution outbox.
SQLite WAL, FULL synchronous mode and explicit transactions provide local
transaction durability subject to the host filesystem's guarantees. The Store
protocol separates persistence from service operations; it currently exposes a
transactional SQL connection. PostgreSQL is a future implementation, not drop-in
verified compatibility.

A worker claims intent and commits before invoking an executor. Pending work is
recoverable after restart. Claimed work is fenced by executor, hub, principal,
profile, task, run and claim identity. Expired leases become uncertain instead of
being redispatched. Replies create an explicit continuation only if the executor
advertises the capability and the task is awaiting input. A text reply cannot
resolve approval.

Executor result and delivery intent commit together. Notification delivery has
its own claim and outcome. A lost network acknowledgement is uncertain even if
the task itself succeeded. No external side effect is made atomic with SQLite.

## Implemented state transitions

- `queued` → `running` when dispatch is claimed
- `queued` → `cancelled` on cancel; `failed` if expired before dispatch
- `running` → `succeeded`, `failed`, `awaiting_input`, `blocked`, or `result_uncertain`
- `running` → `cancel_requested`; this does not assert stopping
- `cancel_requested` → executor-confirmed result or `result_uncertain`
- `awaiting_input` → new `queued` run on an owned explicit reply, or `cancelled`
- expired claim → `result_uncertain`; late results are audited, not replayed

`awaiting_approval`, artifact storage and approval resolution are reserved
protocol concepts. No approval-capable executor or approval action is advertised
by this milestone. `blocked` has no automatic resume.

## Persistence schema

Implemented: meta, principals, tasks, runs, messages, external_refs, inbox,
executor_inbox, outbox, events, leases and rate_limits. Client/transport/executor
registries are trusted runtime configuration, not editable request data.
Artifacts/approvals and persistent registries await their corresponding adapter
milestones. Schema version 1 is checked; newer schemas fail closed.

## Operational constraints

- Run on a private local filesystem, never an NFS/shared multi-node SQLite volume.
- Keep database directory owner-only; existing directory permissions remain the
  operator's responsibility. Task input/output may be confidential.
- Core limits: request 32 KiB, result 64 KiB, 4-hop limit, 24-hour task expiry
  ceiling, 300-second run timeout ceiling, 100 active tasks and 60 accepted
  new-run requests (create/reply)/minute per principal/profile by default.
- Only Mock is verified now. An Executor must enforce its own wall-clock and
  output limits; Core validates results but cannot sandbox a blocking adapter.
- SQLite backup API produces a consistent database snapshot. Restore only while
  stopped. An old snapshot may contain queued work that executed after the backup;
  before starting workers, reconcile every nonterminal restored task with the
  executor. Backup restore cannot recreate effect history absent from the snapshot.
  Claimed work retains its conservative recovery semantics.
