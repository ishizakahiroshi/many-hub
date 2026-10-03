# MANY Hub

**Many tools. Many agents. One hub.**

MANY Hub routes work and results between interchangeable clients, transports and
executors. It is an independent Apache-2.0 project. The authoritative product
specification is [MANY_HUB_MASTER_PLAN.md](MANY_HUB_MASTER_PLAN.md).

## Current milestone: P0–P3 foundation; deployment preparation

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
created. The Slack library adapter is offline verified using fake events/API and the
actual official SDK with networking blocked. It is not connected to a workspace
and has made no live posts. See [Adapter contracts](docs/adapters.md). Its durable
thread mapping, sender grants and run-bound replies remain outside Core. The POSIX fixed-command Executor is locally verified with a real JSON subprocess
and a full CLI create → worker → result round-trip. Explicit trusted operator
configuration is required; it is never enabled by task text. Run
`python examples/command_roundtrip.py` for an effect-free real-process demo.
See [the command contract](docs/adapters.md#generic-fixed-command-executor-p3).
A non-root, no-network Docker/Compose candidate and CI build/smoke job are
included. See [deployment, backup, restore and rollback](docs/deployment.md).
This is local/container preparation; no production VPS deployment occurred. This is an early development snapshot, not a released production hub.

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

## Remaining gates

- Slack needs an explicitly authorized real workspace/identity round-trip
- External AI/bot adapters and MANY-AI-CLI/Deskly integrations are unimplemented
- Remote HTTP/MCP auth, OAuth, team operation and persistent secret provisioning are unimplemented
- Native executable bundles, registry publication and releases have not occurred
- Production deployment, public ingress and Cloudflare changes require separate approval

See the [support matrix](docs/compatibility.md) for exact verified versus unverified
scope. GitHub Actions results are evidence for their exact commit, not a release.
