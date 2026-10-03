# Compatibility and milestone acceptance

Checked 2026-10-03 in a Linux cloud workspace: CPython 3.12.14, SQLite 3.53.1.

| Component / path | Status | Evidence / limit |
| --- | --- | --- |
| Core Service API | verified locally | stdlib unittest suite |
| SQLite/inbox/outbox/events | verified locally | atomicity, duplicate/concurrency, reopen and backup tests |
| Mock Client → Mock Executor → Result | verified locally | tests and examples/mock_roundtrip.py |
| CLI / MCP | P1 pending | SDK 1.29.0 available, not yet a supported adapter |
| Slack | P2 pending | no scopes, credentials or live posting performed |
| Generic fixed-command Executor | P3 pending | safety design in ADR-0004 |
| dot/Dots, Grok, OpenClaw, Hermes | unverified | no product-specific assumptions in Core |
| MANY-AI-CLI / Deskly | deferred | references/API-only integration design |
| VPS Docker | planned | no deployment/build run yet |
| Windows/macOS executables | unverified | future OS-specific bundles and CI needed |
| Multi-user / multi-tenant service | unsupported | owner/profile fields are future isolation preparation |
| Remote MCP / OAuth | unsupported | stdio design only |

## P0 regression coverage

`python -m unittest discover -s tests -v`: 28 Core tests + 3 actual crash-process recovery tests pass (31 P0 tests; P1 is a separate milestone).

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
