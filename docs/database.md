# Database

Operational persistence for `sports-analytics-local` uses local SQLite via the
standard-library `sqlite3` module. Analytical sports datasets live in immutable
Parquet snapshots; this document describes the SQLite operational foundation
only.

## Roles

- **SQLite** stores operational state: application metadata, jobs, job events,
  worker instances, snapshot metadata, audit events, and migration history.
- **Parquet** stores historical and analytical football snapshots under
  versioned directories. `SnapshotRepository` stores metadata only and never writes
  Parquet files itself.

## Connection ownership

- Use `connect_database(path)` as a context manager.
- Each unit of work acquires an explicit connection; there is no module-level
  global connection.
- Do not share one `sqlite3.Connection` across threads.
- Do not set `check_same_thread=False` to bypass ownership rules.
- Always close connections through the context manager.
- Opening/configuration failures become `DatabaseConnectionError`.
- Exceptions raised by caller SQL or repository code inside the `with` body
  propagate unchanged and are not reclassified as connection failures.
- Header inspection uses a focused 16-byte binary read helper. Never use
  `Path.read_bytes()` for database-header checks.

Writable connections:

- create the SQLite parent directory when needed;
- set `row_factory = sqlite3.Row`;
- enable foreign keys and verify they are active;
- set `busy_timeout`;
- prefer WAL journal mode and fail clearly if WAL cannot be enabled;
- use `synchronous=NORMAL`;
- use explicit transaction control (`isolation_level=None` plus `BEGIN`);
- create a new database only when the configured path does not exist;
- reject any **existing** path whose 16-byte header is not exactly the SQLite
  header, including empty files, truncated files, partial headers, and arbitrary
  non-SQLite content;
- leave rejected existing files byte-for-byte unchanged and do not create WAL or
  SHM sidecars for them.

Read-only connections:

- open with SQLite URI `mode=ro`;
- never create the database or parent directories;
- never alter journal mode;
- fail clearly when the file is missing or not a SQLite database.

## Transaction ownership

```text
connection context  -> owns connection lifetime
transaction context -> owns commit / rollback
repositories        -> own neither
```

- Use `transaction(connection)` or `transaction(connection, immediate=True)`.
- Commit only after the caller body completes successfully.
- Roll back when the caller body raises.
- If `commit()` itself raises, attempt rollback while the transaction remains
  active, preserve and re-raise the original commit exception, and do not let a
  rollback-cleanup failure mask that commit failure.
- After a failed commit that SQLite can roll back, `connection.in_transaction`
  is false and the connection remains reusable.
- Rollback and connection-close cleanup must preserve an already-active primary
  exception from the caller body; cleanup failures must not replace it. After a
  successful caller body, a close failure may propagate normally.
- Nested independent transactions are rejected.
- Repository methods must not call `commit` when the caller owns the transaction.
- Multi-write operations must share one explicit transaction.
- Read operations may run without an explicit write transaction.
- Public repository write methods **actively enforce** an open transaction via
  `require_active_transaction(...)` before mutating state. Calling them outside
  `transaction(...)` raises `RepositoryError` and writes nothing.
- Worker queue operations are mediated by `WorkerService`, which opens a
  short-lived connection per operation and owns the required transaction. Claim,
  recovery, lease renewal, completion, failure, and worker-status mutations use
  `transaction(connection, immediate=True)` where write contention matters.
- Job handlers run outside SQLite transactions and never share a connection with
  the worker heartbeat thread.

## Migrations

Migrations are forward-only, packaged SQL files discovered with
`importlib.resources` from `sports_analytics.data.sql.migrations`.

Filename pattern:

```text
0001_initial.sql
0002_worker_runtime.sql
```

Rules:

- strictly increasing consecutive integer versions starting at 1;
- stable name derived from the filename;
- SHA-256 checksum over normalized migration text;
- deterministic ordering independent of filesystem listing;
- no recursive discovery of arbitrary SQL files;
- no Python migration modules;
- every migration sequence (packaged or explicitly supplied) is validated for
  version, filename/name match, checksum shape, checksum/SQL consistency, and
  SQL safety before use.

The runner creates `schema_migrations` before applying packaged migrations and
records:

- `version`
- `name`
- `checksum`
- `applied_at` (canonical UTC)
- `execution_time_ms`

Applied history must be exactly the consecutive prefix `1..current_version`.
Gaps, duplicates, non-monotonic versions, missing earlier versions, newer-than-
packaged versions, name mismatches, and checksum mismatches all raise
`DatabaseMigrationError`.

Migration locking uses `BEGIN IMMEDIATE` so concurrent local processes cannot
falsely double-apply migrations. Each migration's SQL and metadata insert happen
in the same transaction. `executescript` is not used; statements are split with a
quote/comment-aware parser aided by `sqlite3.complete_statement` and executed
individually so transaction boundaries remain intact.

The splitter appends each `;` into the current buffer and only finalizes when
`sqlite3.complete_statement` reports completeness. That supports valid compound
SQLite statements such as `CREATE TRIGGER ... BEGIN ... END;` with internal
semicolons. SQL comments are treated as lexical whitespace during splitting and
execution preparation: `--` line comments behave like newlines, and `/* ... */`
block comments contribute separating whitespace so adjacent tokens are never
concatenated. Semicolons and prohibited keywords inside comments do not change
statement boundaries or safety checks. Trailing whitespace and trailing
comment-only content are ignored. A final statement may omit a terminating
semicolon when it becomes complete after one is appended; genuinely incomplete
trailing SQL, unclosed quotes, and unclosed block comments are rejected.

Migration checksums are SHA-256 digests of the original normalized source SQL.
Parsing must not alter the SQL's lexical semantics relative to that source text.

Every migration definition is runtime-type-validated before numerical sorting.
Malformed objects raise `DatabaseMigrationError` rather than leaking built-in
`TypeError` / `AttributeError` failures from sort keys.

Migration SQL must not begin (after whitespace, `--` / `/* */` comments, and an
optional UTF-8 BOM) with any of:

- `BEGIN`, `COMMIT`, `END`, `ROLLBACK`, `SAVEPOINT`, `RELEASE`
- `VACUUM`, `ATTACH`, `DETACH`
- **any** `PRAGMA` (all packaged migration PRAGMA statements are prohibited;
  connection safety PRAGMAs belong in `database.py`)

Words appearing only inside quoted strings, quoted identifiers, comments, or
later in the body of a different valid statement are not treated as prohibited
first tokens.

Failures while locating, enumerating, inspecting, or reading packaged migration
resources (including UTF-8 decoding failures) raise `DatabaseMigrationError`
with exception chaining. Messages identify discovery vs reading and the package
or filename when known; they do not expose migration file contents.

Applied migrations are immutable:

- checksum and name are verified before applying newer migrations;
- mismatches raise `DatabaseMigrationError`;
- history is never rewritten automatically;
- never edit an applied migration file after it has shipped.

Current packaged migrations:

| Version | File | Checksum |
| --- | --- | --- |
| 1 | `0001_initial.sql` | `404e1c0b36390ff7a42de901f344edcb60b9cee248b741116bc9d47a17cf48de` |
| 2 | `0002_worker_runtime.sql` | `94af0d6d9df740ac0c578c815015fe3981acfc48f5faa3cfb1ba3bc1a719b55d` |
| 3 | `0003_snapshot_source_deduplication.sql` | `84fda02807a42e9e951d4fad4e8bedeecd1a2fda675be929762394ac5cc2ec94` |
| 4 | `0004_settlement_monitoring_governance.sql` | `8559eecc1565808578ab402250481e94f15d31a49b7714ff43c4b413702ef11d` |

Migration `0001_initial.sql` is unchanged. Schema version `2` adds the durable
worker runtime metadata and queue lease integrity described below.

## Automatic startup migration

Normal runtime bootstrap:

1. load settings
2. resolve paths
3. create runtime directories
4. seed deterministic generators
5. configure logging
6. ensure SQLite is migrated (`ensure_database_ready`)
7. return `RuntimeContext` with `database_path` and `schema_version`

`RuntimeContext` does **not** retain an open SQLite connection.

`--validate-config` remains side-effect free: no directories, no SQLite file, no
migration table, no log file, no random seeding.

## Database CLI options

All five root scripts support mutually exclusive modes:

| Option | Behaviour |
| --- | --- |
| `--validate-config` | Validate configuration only |
| `--database-status` | Read-only inspect an existing database |
| `--migrate-database` | Create parent directory if needed and apply migrations |

`--database-status` requires the database file to exist, creates nothing, applies
no migrations, and prints:

```text
database valid: path=... current_version=2 latest_version=2 pending=0
```

Expected database failures return exit code `2` with a concise stderr message and
no traceback.

## Initial tables

Migration `0001_initial` creates:

- `application_metadata`
- `jobs`
- `job_events`
- `snapshots`
- `audit_events`

Migration `0002_worker_runtime` creates:

- `worker_instances`

and adds worker/lease indexes:

- `idx_worker_instances_status_heartbeat`
- `idx_worker_instances_heartbeat`
- `uq_worker_instances_current_job`
- `idx_jobs_running_lease_expires`

It also adds triggers:

- `trg_jobs_running_lease_insert`
- `trg_jobs_running_lease_update`
- `trg_worker_instances_current_job_insert`
- `trg_worker_instances_current_job_update`

After installing the lease triggers, migration `0002` validates every existing
`jobs` row with a no-op update of the trigger-covered columns. A legacy v1
running job with a NULL lease aborts the migration atomically (`DatabaseMigrationError`),
leaving schema version 1 and no `worker_instances` objects behind.

At schema version 2 no sports-domain SQLite tables existed. Migration 0004 keeps
analytical datasets in immutable artifacts while adding only minimal operational
result, settlement, monitoring, and model-role state.

### Job priority

Lower integer means higher priority. Default priority is `100`. Worker claim
selection orders available pending jobs by:

1. `priority` ascending
2. `available_at` ascending
3. `created_at` ascending
4. `id` ascending

Running jobs must hold a complete lease. The `0002` triggers enforce the lease
invariant at the database boundary:

- `status = 'running'` requires both `lease_owner` and `lease_expires_at`;
- any non-running status must have both lease columns NULL.

The partial `idx_jobs_running_lease_expires` index supports recovery scans for
expired running-job leases ordered by `lease_expires_at`, `updated_at`, and `id`.

The partial unique `uq_worker_instances_current_job` index enforces that a
non-NULL `worker_instances.current_job_id` is associated with at most one worker
row. Claiming a job also requires the claiming worker's own `current_job_id` to
be NULL, so an occupied worker cannot claim a second job.

The `trg_worker_instances_current_job_*` triggers require any non-NULL
`current_job_id` to reference a matching `jobs` row that is `running`, leased by
that worker (`lease_owner = worker_instances.id`), and has a non-NULL
`lease_expires_at`. Clearing `current_job_id` remains allowed for worker failure,
shutdown interruption, and expired-lease recovery. Ordinary heartbeats must not
change `current_job_id`; only queue claim, finalization, recovery, and terminal
worker transitions may.

Retry transitions (`running|failed -> pending`) require an explicit
`retry=True` argument. Ordinary `transition_job` calls cannot retry. Retries are
rejected when `attempts >= maximum_attempts`, clear lease fields and
`finished_at`, preserve `last_error` for history, increment version once, and
append exactly one event in the same transaction. Starting a job
(`pending -> running`) is also rejected when attempts are already exhausted.

Worker-driven retries use deterministic backoff:

```text
min(max, base * 2**(attempts-1))
```

where `attempts` is the count after the job was claimed.

### Worker instances

`worker_instances` records durable local worker process state:

- canonical worker UUID (`id`);
- process metadata (`name`, `process_id`, `hostname`);
- lifecycle timestamps (`started_at`, `heartbeat_at`, `stopping_at`,
  `stopped_at`);
- current job pointer;
- status (`starting`, `running`, `stopping`, `stopped`, `failed`);
- capabilities JSON;
- optimistic version.

Worker UUIDs fence job ownership. Lease renewal and finalization require the
current `lease_owner`, expected job version, live lease, and compatible worker
state. Stale workers and expired leases are recovered through explicit worker
service operations; there is no force-cancellation path for a running handler.

During upgrade from schema version 1, `0002_worker_runtime` creates the
running-job lease triggers and then runs a no-op `UPDATE jobs` across existing
rows. This preflight validates legacy jobs against the new invariant before the
migration is recorded. Legacy `running` rows without both `lease_owner` and
`lease_expires_at` abort the migration with `running job requires complete
lease`; the migration transaction rolls back atomically, so no partial
`0002` schema objects or migration-history rows are committed and the invalid
legacy rows remain unchanged for manual repair.

Repository numeric arguments such as priority, attempts bounds, versions,
row counts, limits, and offsets use strict `int` validation (bools, floats, and
numeric strings are rejected). Integer columns also use SQLite `typeof(...)`
checks so REAL values cannot sneak through affinity alone.

### Snapshot READY immutability

Once a snapshot metadata row is `ready`, ordinary repository updates are
rejected. There is no repository delete for ready snapshots.

Snapshot `relative_path` values are validated on raw segments before
normalization: absolute paths, backslashes, repeated/trailing separators, `.` /
`..`, Windows drive forms, UNC-style paths, and NUL bytes are rejected.

### Append-only events

`job_events` and `audit_events` are append-only through repository APIs.

## Canonical JSON and timestamps

Canonical JSON:

- `sort_keys=True`
- `separators=(",", ":")`
- `ensure_ascii=False`
- reject NaN / Infinity and non-JSON objects

Canonical UTC timestamps:

- timezone-aware inputs only
- normalize to UTC
- format `YYYY-MM-DDTHH:MM:SS.ffffffZ`
- application-generated (not SQLite local-time functions)

## Backup warning

Back up `storage/operational.sqlite3` (and any `-wal` / `-shm` sidecars) before
manual database manipulation. Do not edit applied migration history by hand.

## Current limitations

- Analytical datasets remain in immutable snapshots/artifacts; SQLite stores
  only operational registrations, indexes, audit state, and current model roles.
- Sports-domain job handlers live outside this package (`ingest.football-data-csv`
  in `sports_analytics.ingestion`); SQLite only records job and snapshot
  metadata.
- No bookmaker execution, staking, bankroll, or Streamlit mutation logic.
- No paid API or external AI runtime dependency.


## Migration 0003

Filename: `0003_snapshot_source_deduplication.sql`

Adds operational indexes only. No sports-domain SQLite tables are created.

- `uq_snapshots_active_source_version` — unique partial index preventing two
  active (`building`/`ready`) snapshots for the same
  `(snapshot_type, source_name, source_version, schema_version)`.
- `idx_snapshots_source_version_status` — lookup index for source identity and
  status.

Failed snapshots are excluded from the unique key so replacements can proceed.
Migrations `0001` and `0002` remain immutable.

## Migration 0004

`0004_settlement_monitoring_governance.sql` adds minimal operational tables for
verified result snapshot registration, immutable analytical settlement
runs/evidence/audit events, monitoring run/finding indexes, verified model
registry state, immutable promotion decisions, and audited role transitions.
Partial uniqueness enforces one active champion per exact sport/market scope.
Decimal unit values are stored as text. Migrations `0001`–`0003` remain
byte-for-byte unchanged.
