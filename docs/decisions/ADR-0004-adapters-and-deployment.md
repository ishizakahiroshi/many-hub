# ADR-0004: Adapter contracts, first executor, and deployment

Date: 2026-10-03. Status: accepted for v0.1.

Core knows Client, Transport, Executor, task/request/run IDs, a validated payload,
capabilities, ownership and opaque external references. Product IDs and API
semantics belong in adapter storage. Adapters translate authenticated input into
the same Service API; none may inject a principal or grant through task input.
Executor requests contain bounded JSON and an absolute deadline, not hub tokens
or environment secrets. Executor results are schema-checked and fenced to a run.

Start with a deterministic Mock Executor. The first real executor is a generic
fixed-command process runner: administrator-configured absolute executable and
fixed argv, JSON stdin, no shell, restricted environment, dedicated working
directory, timeout and output bound. Task data cannot choose the executable,
arguments, environment or path. This is not a sandbox for hostile programs;
operators must trust the allowlisted program. Unknown effect outcomes are not
retried. AI headless CLIs were considered but require unverified credentials,
product-specific interaction and effect policy, so are deferred.

Local operation and one VPS Docker service use the same Core. Persist one local
volume, run non-root, use read-only container root and bounded resources, omit the
Docker socket, and expose only loopback. Cloudflare is optional ingress later;
Core has no Workers dependency. No production deployment or DNS/Access/Tunnel
change occurs under this decision.

MANY-AI-CLI and Deskly connect through explicit, minimal external references or
published APIs. No internal DB access, token reuse, bulk session import, arbitrary
PTY input, code copying or product repository edits. External AI/bot capabilities
remain unverified until their real end-to-end test is authorized and succeeds.
