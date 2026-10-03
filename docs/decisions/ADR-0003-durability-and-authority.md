# ADR-0003: Durable single-node Core and explicit authority

Date: 2026-10-03. Status: accepted for v0.1.

Use SQLite on a local disk, WAL, foreign keys, FULL synchronous mode, bounded
busy timeout, and explicit `BEGIN IMMEDIATE` transactions behind a Store
interface. Store principal/profile ownership on every aggregate. Atomically
persist task, run, request/event deduplication, immutable events, and dispatch
outbox. A request ID replay with different content is a conflict, not a new task.

A worker atomically claims a pending dispatch with a lease, commits, then calls
an Executor. Expired claimed work becomes `result_uncertain`; it is never
requeued automatically. A crash before a claim is safe to resume. A crash after
claim but before external execution may conservatively become uncertain. This
trades availability for side-effect safety. There is **no universal exactly-once
or at-most-once external effect guarantee**. An external executor can need its
own durable idempotency key and reconciliation protocol. Late results are audited
without changing terminal/uncertain state. Delivery state is separate from task
state; ambiguous delivery is not blindly retried either.

Authority originates in configured, authenticated adapter context, never request
JSON, model output, chat text, or an `approved: true` flag. Apply profile/principal
ownership plus action and executor grants. Local CLI/stdio runs under the local
OS user's file permissions. Optional HTTP binds loopback by default and requires
an externally supplied credential mapped to one principal; no generated or
shared universal token. Public binding and remote MCP are out of scope. Trusted
adapter completion must bind executor, run, and claim identifier.

Approval records are informational routing only until an executor-owned,
identity-bound, expiring, single-use approval protocol is implemented. Text
replies do not authorize effects. All unsupported capabilities default false.

Sources: https://sqlite.org/atomiccommit.html ; https://sqlite.org/wal.html ;
https://docs.python.org/3.12/library/sqlite3.html . SQLite WAL requires a local
filesystem, not an NFS/multi-node volume. Backup uses SQLite's backup API.
