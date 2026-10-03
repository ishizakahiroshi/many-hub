# Compatibility and milestone acceptance

Checked 2026-10-03 in a Linux cloud workspace: CPython 3.12.14, SQLite 3.53.1.

| Component / path | Status | Evidence / limit |
| --- | --- | --- |
| Core Service API | verified locally | stdlib unittest suite |
| SQLite/inbox/outbox/events | verified locally | atomicity, duplicate/concurrency, reopen and backup tests |
| Mock Client → Mock Executor → Result | verified locally | tests and examples/mock_roundtrip.py |
| CLI / MCP stdio | verified locally | 14 interface tests, official SDK 1.29.0 client/server; no remote MCP |
| Slack library adapter | offline verified | 39 fixture/SDK tests; live connection, reconnect and posting unverified |
| Generic fixed-command Executor | P3 pending | safety design in ADR-0004 |
| dot/Dots, Grok, OpenClaw, Hermes | unverified | no product-specific assumptions in Core |
| MANY-AI-CLI / Deskly | deferred | references/API-only integration design |
| VPS Docker | planned | no deployment/build run yet |
| Windows/macOS executables | unverified | future OS-specific bundles and CI needed |
| Multi-user / multi-tenant service | unsupported | owner/profile fields are future isolation preparation |
| Remote MCP / OAuth | unsupported | stdio design only |

## P0 regression coverage

`python -m unittest discover -s tests -v`: 85 tests pass: 29 Core + 3 actual crash-process recovery + 14 CLI/MCP + 39 Slack fixture/SDK tests (install both optional extras to run every SDK check).

Coverage includes mock result + independent delivery, idempotent replay and ID
conflicts, duplicate callbacks, expired input, result deadlines, queued and
claimed restart paths, no automatic uncertain retry, late-result audit,
cancellation request vs confirmation, owned explicit question replies,
principal/profile/grant isolation, spoofed authority and routes, callback
fencing, payload/Unicode/hop/expiry bounds, durable quotas, delivery uncertainty,
secret-bearing exception suppression, output bounds, schema guard, consistent
backup, immutable events, rollback, competing workers and concurrent creates.

Not proven: real external side-effect deduplication, live Slack reconnect,
production system shutdown/restart, non-Linux execution, remote authentication,
OS-level sandboxing or production readiness. Tests deliberately do not claim
those properties.

P1 additionally verifies real stdio initialize/list/call/ping, owned question/reply/cancel,
strict arguments, malformed-frame sanitization, and Core/CLI without MCP imports.

P2 adds an optional expected_run_id fence on task replies, verified question-post
timestamps for Slack continuation, and durable adapter-owned inbox/thread mapping.
P0 Windows CI exposed a test-only unclosed SQLite connection; the test now uses
explicit closing. Exact new-head CI must pass before Windows verification is claimed.
