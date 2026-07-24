# Database

Operational persistence for `sports-analytics-local` uses local SQLite via the
standard-library `sqlite3` module. Analytical datasets will use Parquet in later
phases; this document describes the current SQLite foundation only.

## Roles

- **SQLite** stores operational state: application metadata, jobs, job events,
  worker instances, snapshot metadata, audit events, and migration history.
- **Parquet** (future) will store historical and analytical datasets under
  versioned snapshot directories. Repository methods do not write Parquet files.

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
| 2 | `0002_worker_runtime.sql` | `b3a8d93ae81ce2e21ae9e74a420bf598b345d63fe4ed11d4d84ced6302021faa` |

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
- `idx_worker_instances_current_job`
- `idx_jobs_running_lease_expires`

It also adds triggers:

- `trg_jobs_running_lease_insert`
- `trg_jobs_running_lease_update`

No sports-domain tables (teams, fixtures, odds, predictions, bets, etc.) exist
yet.

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

- No sports-domain job handlers.
- No Parquet writers.
- No sports-domain schema.
- No scraping, modelling, betting, or Streamlit UI logic.
- No paid API or external AI runtime dependency.
