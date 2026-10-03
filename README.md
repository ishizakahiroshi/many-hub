# MANY Hub

**Many tools. Many agents. One hub.**

MANY Hub routes work and results between interchangeable clients, transports and
executors. It is an independent Apache-2.0 project. The authoritative product
specification is [MANY_HUB_MASTER_PLAN.md](MANY_HUB_MASTER_PLAN.md).

## Current milestone: P1 local CLI and MCP

Implemented and locally tested: product-independent Service API, SQLite
persistence, tasks/runs/events, durable inbox/outbox, Mock Executor, owned JSON
replies, cancellation requests, deduplication, quotas, loop/expiry checks, backup
and conservative restart recovery. No third-party package is needed for Core.

No universal exactly-once external side-effect guarantee is claimed. If a worker
crashes after dispatch is claimed, an expired lease becomes `result_uncertain`
and is never automatically replayed. Task success and notification delivery are
separate states.

Python 3.12+ is required. Run from the repository root:

```sh
python -m unittest discover -s tests -v
python examples/mock_roundtrip.py
```

The example uses a temporary database. It creates one task, runs Mock Executor,
reads the structured result and acknowledges a mock delivery. No service
credentials, network access, other product, or AI account is needed.

CLI and official MCP stdio are implemented and locally tested. Examples:

```sh
python -m manyhub capabilities --json
python -m manyhub task create --input-file request.json --json
python -m manyhub worker --once --json
python -m manyhub task list --json
python -m manyhub serve --local
```

A create request is JSON such as `{"request_id":"example-1","input":{"text":"hello"}}`.
For MCP, install the optional extra (`pip install '.[mcp]'`) then run
`manyhub mcp --stdio`. MCP queues tasks; a separate local worker processes them.
See [Protocol](docs/protocol.md). No network listener or remote credential is
created. Slack (P2), real fixed-command execution (P3) and container packaging
remain subsequent milestones. This is an early development snapshot, not a released production hub.

## Design and boundaries

- [Architecture](docs/architecture.md)
- [Security](docs/security.md)
- [Compatibility and acceptance status](docs/compatibility.md)
- [Decisions](docs/decisions/)

Local adapters construct a trusted `Context` from the OS/configuration. Remote
adapters must authenticate and bind the principal, profile, client, transport and
executor grants before entering the Service. Task JSON cannot supply identity,
authority or executable commands. No adapter approval or live integration is
implied by the presence of an interface.

GitHub is the canonical implementation history. No package registry publication,
release, merge or production deployment is part of this milestone.
