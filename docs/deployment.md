# Local operation and VPS/Docker preparation

No production deployment, package publish, OAuth, token creation, public ingress,
or Cloudflare configuration is performed by this repository. The container is a
local worker for Mock tasks; Slack and command execution are not enabled by the
container entrypoint. This is not an Internet-facing service or team release.

## Local

Install Python 3.12+ and install this project (`python -m pip install .`) or use
`python -m manyhub` from the checkout. Runtime Core has no third-party dependency.
Use a private directory for the database:

```sh
manyhub --database /private/path/manyhub.sqlite3 capabilities --json
manyhub --database /private/path/manyhub.sqlite3 serve --local
```

In another process, create/get/list/reply/cancel against that same database. MCP
requires the optional `mcp` extra and uses stdio only. It queues tasks for a
separate local worker; it does not grant remote access or start an AI account.

## Docker/VPS candidate

The Dockerfile uses an official Python base, a non-root fixed UID and no optional
SDKs. Compose has a read-only root, one persistent local volume, no capabilities,
no new privileges, memory/CPU/process limits, no network and no host ports.

```sh
docker compose build
docker compose up -d
docker compose exec hub manyhub --database /data/manyhub.sqlite3 capabilities --json
docker compose logs --tail 50 hub
```

These commands are operator instructions, not evidence of a production deploy.
Do not change `network_mode`, mount secrets or add Slack until the specific
service access has been authorized and reviewed. No Docker socket is required.
For a VPS, keep the data volume on local disk, monitor free disk space and retain
host security updates. SQLite is single-node; shared/NFS storage is unsupported.

The base image tag is not an immutable release pin. Before any authorized release,
select and record a reviewed digest, scan dependencies/base, generate checksums,
and test the exact image on the target architecture. No image is pushed by CI.

## Backup

Stop workers for the simplest maintenance procedure, or use the SQLite backup API
for a transactionally consistent online snapshot. `SQLiteStore.backup(path)`
refuses an existing destination and creates an owner-only file. Copying only the
live `.sqlite3` file while WAL writes continue is not a safe backup procedure.
Keep backups private; they can contain confidential task input/results.

## Restore

Restore is an offline operator operation. Preserve the current database and WAL
files, stop **all** CLI/MCP/worker/adapter processes using the database, and verify
the backup with `PRAGMA integrity_check` before replacing the data volume.

An old snapshot can say `queued` for work that ran after it was captured. Before
starting any restored worker, reconcile **every nonterminal restored task** with
its real executor or quarantine it for manual review. SQLite alone cannot recover
external side-effect history absent from the snapshot. Claimed work becomes
uncertain when its lease expires; do not reset it to queued to force a retry.
Recovery tests prove transaction behavior, not universal disaster-replay safety.

## Update and rollback

1. Record current image digest/source commit and SQLite schema version.
2. Stop work intake and workers. Resolve or document in-flight/uncertain runs.
3. Take and validate a private backup; keep the previous image/source available.
4. Test the candidate against a **copy** with external executors disabled.
5. Start the authorized version, inspect health and task/delivery states.

Rollback code only if it supports the current schema. Startup rejects newer
schema versions; do not force a downgrade. Restoring an older data snapshot is a
separate recovery operation with the reconciliation risks above. Never perform a
production update or restore solely because these instructions exist.

## Distribution and verification limits

Local wheel builds are validated. OS-specific executable bundles are a future
PyInstaller onedir workflow, not shipped artifacts. Windows command execution is
unsupported until process-tree containment is verified; Core/CLI are portable
Python. GitHub CI covers OS/Python combinations and optional SDK fixtures; consult
the exact commit's run before calling that platform verified.

HTTPS, scoped remote auth, secret provisioning, automated restore/reconciliation,
remote MCP OAuth and multi-user operation remain unimplemented. Cloudflare is
optional future ingress, not a Core dependency.
