# MANY Hub

**Many tools. Many agents. One hub.**

MANY Hub routes work and results between interchangeable clients, transports and
executors. It is an independent Apache-2.0 project. The authoritative product
specification is [MANY_HUB_MASTER_PLAN.md](MANY_HUB_MASTER_PLAN.md).

## Current milestone: P0 Core

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

CLI/MCP (P1), Slack (P2), a real fixed-command executor (P3), and container
packaging (P6 preparation) are subsequent milestones, not current verified
features. This is an early development snapshot, not a released production hub.

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
