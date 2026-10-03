# ADR-0005: Runtime-aware SQLite journaling

Date: 2026-10-03. Status: accepted; refines ADR-0003's unconditional WAL choice.

SQLite documents a rare multi-connection WAL-reset corruption issue. Upstream
fixes are 3.51.3+ and the 3.44.6/3.50.7 maintenance branches. Python's version does
not establish its linked SQLite version.

Use integer-version comparisons to select WAL only for known fixed runtimes.
Otherwise use DELETE rollback journaling with synchronous=EXTRA. EXTRA includes
the directory synchronization needed for rollback-journal commit durability;
WAL keeps synchronous=FULL. Unknown distribution backports take the conservative
fallback. Verify the actual selected mode and fail closed if it is unsafe.

This costs reader/writer concurrency and extra synchronization on older runtimes.
It does not change task transactions, effect uncertainty, backup requirements or
restore reconciliation. Stop all hub processes before changing linked runtimes or
journal modes; never mix runtimes against one live database. Host filesystem and
storage guarantees still matter. Initialization failures close their connection.

Sources checked:
- https://sqlite.org/wal.html#the_wal_reset_bug
- https://sqlite.org/pragma.html#pragma_synchronous

No third-party database package, credentials, deployment or schema migration is
introduced. Tests cover version boundaries, fallback durability settings,
transaction rollback, backup/reopen, uncertain-claim recovery and cleanup.
