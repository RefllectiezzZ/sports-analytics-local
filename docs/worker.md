# Worker and job queue

This document describes the durable local job worker introduced for
`sports-analytics-local`. The worker is SQLite-backed, sequential, and intended
for single-user localhost operation.

## Scope

Implemented now:

- durable `worker_instances` metadata;
- atomic job claiming with lease ownership;
- lease heartbeat renewal;
- expired-lease recovery;
- deterministic retry backoff;
- static in-process handler registry;
- sequential worker loop;
- worker CLI operations;
- `run_local.py` supervising the worker child only.

Not implemented in this release:

- Streamlit child process;
- modelling, predictions, or betting jobs;
- parallel job execution inside one worker process;
- cooperative cancellation of an already-running job handler.

## Queue lifecycle

Jobs are stored in the operational SQLite `jobs` table and audited through
append-only `job_events`.

Typical flow:

1. create a pending job through `JobRepository.create_job`;
2. a running worker claims it atomically (`pending -> running`);
3. the handler executes outside any open SQLite transaction;
4. the worker finalizes success, schedules a retry, or records terminal failure;
5. expired leases are recovered into pending or failed when a worker disappears.

Claim selection order is exactly:

1. `priority ASC` (lower integer is higher priority);
2. `available_at ASC`;
3. `created_at ASC`;
4. `id ASC`.

## Worker lifecycle

Each worker process:

1. generates or accepts one canonical UUID;
2. registers a `worker_instances` row as `starting`;
3. transitions to `running` after initialization;
4. reconciles stale workers and recovers expired leases;
5. polls, claims, executes, and finalizes jobs sequentially;
6. transitions to `stopping` then `stopped` on graceful exit;
7. transitions to `failed` on fatal worker errors.

Allowed worker statuses: `starting`, `running`, `stopping`, `stopped`, `failed`.

## Worker UUID fencing

The worker UUID is:

- unique per process start;
- never reused after restart;
- stored as lowercase canonical UUID text;
- constant for the lifetime of one worker process;
- used as the job `lease_owner`.

Process ID, hostname, username, or static config strings are not sufficient lease
tokens. Finalization and heartbeat operations must match the current
`lease_owner`. A paused or stale process cannot finalize a job after another
worker has recovered and reclaimed it.

## Atomic claim

`JobQueueRepository.claim_next_job` must run inside
`transaction(connection, immediate=True)`.

On claim the repository:

- selects at most one eligible pending job;
- increments `attempts` by exactly one;
- sets `lease_owner` and `lease_expires_at`;
- sets `started_at` for the current attempt;
- clears `result_json` and keeps `finished_at` NULL;
- increments job version once;
- appends exactly one `claimed` event;
- updates the worker `current_job_id` and heartbeat in the same transaction.

Eligibility requires `status=pending`, `available_at <= claimed_at`,
`attempts < maximum_attempts`, no lease, and a running worker whose
`current_job_id` is NULL. A worker that already points at a current job is
considered occupied and cannot claim another job.

The database also enforces a unique current-job association: at most one
`worker_instances` row may reference a given non-NULL `current_job_id`.

Queue operations are the exclusive owner of `current_job_id`. Ordinary worker
heartbeats update only `heartbeat_at` and the worker version; they cannot assign,
replace, or clear the current-job pointer. Claim, success/failure finalization,
expired-lease recovery, and terminal worker transitions remain the only paths
that change the association. The database also rejects any non-NULL
`current_job_id` that does not point at a matching `jobs` row that is `running`,
leased by that worker (`lease_owner = worker.id`), and has a non-NULL
`lease_expires_at`. Clearing `current_job_id` to NULL remains allowed so worker
failure, shutdown interruption, and recovery can detach the worker while leaving
the job lease for later recovery.

## Lease ownership

Lease duration uses `worker.stale_job_timeout_seconds`.

A worker may renew, complete, or fail a job only when all of the following hold:

- job status is `running`;
- `lease_owner` equals the caller worker UUID;
- `lease_expires_at` is later than the operation timestamp;
- worker `current_job_id` matches the job;
- worker is `running` or `stopping`;
- expected job lifecycle version matches when required.

Rejected stale finalization writes nothing and appends no event.

The runner additionally treats locally observed lease loss as a hard fence. If
`context.checkpoint()`, the heartbeat thread, or heartbeat cleanup reports lease
loss at any point before finalization, including after the handler's final
checkpoint, the worker does not complete, fail, or retry the job. It leaves the
running lease for recovery, marks the worker failed, and terminates instead of
claiming more work in that process.

## Heartbeat

While a handler runs, a daemon heartbeat thread renews the lease using short-lived
SQLite connections. The handler never shares a connection with the heartbeat
thread. Ordinary renewals:

- extend `lease_expires_at`;
- update job `updated_at` and worker `heartbeat_at`;
- do **not** increment the job lifecycle version;
- do **not** append job events;
- do **not** change `current_job_id`.

Idle `heartbeat_worker` calls likewise preserve `current_job_id` exactly.

Installed worker signal handlers (`SIGINT`, `SIGTERM`, and `SIGBREAK` when
available) only set a `threading.Event`. They must not log, acquire locks, touch
SQLite, or mutate `JobExecutionContext`. Stop propagation into the active
execution context happens outside the signal callback via the heartbeat
controller's `should_stop` path and the normal worker loop.

Lifecycle version changes occur on claim, retry, recovery, cancellation, success,
and terminal failure.

## Lease expiry and recovery

`recover_expired_leases` runs inside `BEGIN IMMEDIATE` and selects running jobs
with `lease_expires_at <= recovered_at`, ordered by:

1. `lease_expires_at ASC`;
2. `updated_at ASC`;
3. `id ASC`.

If attempts remain, the job is requeued as pending with deterministic backoff and
a `lease_expired_requeued` event. Otherwise it becomes terminal failed with a
`lease_expired_failed` event. Recovery clears the previous owner's
`current_job_id` only when it still points at the recovered job.

Repeated recovery at the same timestamp is idempotent.

## Deterministic backoff

Retry delay has no jitter:

```text
delay = min(
    retry_backoff_max_seconds,
    retry_backoff_base_seconds * 2 ** (attempts - 1),
)
```

`attempts` is the count after the job was claimed. The first failed attempt uses
the base delay. `available_at = failed_at + delay`.

## At-least-once semantics

The queue provides local **at-least-once** execution:

- a job may run again after a crash and lease expiry;
- future handlers must be designed to be idempotent;
- `idempotency_key` prevents duplicate job creation, not duplicate execution after
  an ambiguous crash;
- the system does **not** claim exactly-once semantics;
- lease ownership prevents two healthy workers from both successfully finalizing
  the same current claim.

## Handler registry

Handlers are registered by project code in an in-process `HandlerRegistry`.

Do not load executable code from job payloads, database rows, environment
variables, arbitrary module paths, or user-entered import strings.

The registry rejects duplicate job types, validates identifiers, freezes before
worker execution, and iterates job types deterministically. There is no mutable
module-level singleton registry.

Built-in infrastructure handler:

- `system.noop` — no I/O, no sleep, no network, no filesystem or database writes;
  returns deterministic JSON for end-to-end tests.

Do not enqueue `system.noop` automatically. No sports handlers exist yet.

## Handler errors

- `RetryableJobError` schedules a retry when attempts remain;
- `PermanentJobError` produces terminal failure;
- unknown handlers produce terminal failure;
- ordinary `Exception` is retryable by default up to maximum attempts;
- `KeyboardInterrupt` / `SystemExit` are not converted into ordinary job failures.

Stored `last_error` contains a sanitized class name and concise message only.
Tracebacks, payloads, credentials, and environment data are never stored there.

## Graceful shutdown

Stop sources:

- `SIGINT` / `SIGTERM` where available;
- an internal `threading.Event`;
- finite CLI modes (`--once`, `--max-jobs`).

Signal handlers only set the stop event; they do not write to the database.

Idle shutdown stops claiming, marks the worker stopping/stopped, and exits.

Shutdown during a handler:

- stops new claims;
- keeps lease heartbeat alive during the configured grace period, even after the
  stop request is visible to the handler;
- exposes the stop request through `context.checkpoint()`;
- finalizes the job only if the handler completes safely;
- otherwise leaves the running lease to expire for later recovery.

Running jobs are not force-cancelled in this release.

## Worker CLI

```bash
python worker.py --queue-status
python worker.py --recover-expired-leases
python worker.py --once
python worker.py --max-jobs 5
python worker.py --worker-id <uuid>
python worker.py
```

`--queue-status` is read-only: it validates configuration, requires an existing
up-to-date database, creates no directories, starts no worker, and applies no
migrations.

`--recover-expired-leases` bootstraps/migrates as needed, runs one recovery pass,
and does not start a long-running worker.

Shared modes `--validate-config`, `--database-status`, and `--migrate-database`
remain available and are mutually exclusive with worker operational modes.

Example outputs:

```text
queue status: pending=3 available=2 delayed=1 running=1 expired=0 succeeded=10 failed=2 cancelled=1 workers_active=1 workers_stale=0
lease recovery: scanned=2 requeued=1 failed=1
```

## run_local supervisor

`run_local.py` currently supervises **only** the worker child process.

```bash
python run_local.py
python run_local.py --worker-once
python run_local.py --worker-max-jobs 3
python run_local.py --worker-id <uuid>
```

Behaviour:

- bootstraps via `bootstrap_runtime("run_local", ...)` (validated settings, paths,
  logging, and an up-to-date database);
- starts `worker.py` with `sys.executable` and an absolute script path;
- never uses `shell=True`;
- forwards only validated config/env paths and worker flags;
- propagates the child exit code;
- after successful child creation, signal-handler installation and supervision
  share one cleanup boundary so no post-spawn exception can orphan the child;
- installs `SIGINT` / `SIGTERM` / `SIGBREAK` (when available) atomically: a later
  registration failure restores every already-changed handler before re-raising;
- parent signal handlers only set a `threading.Event`; process operations run in
  the supervisor loop;
- requests platform-specific child shutdown on SIGINT/SIGTERM/SIGBREAK exactly
  once per run and kills only after `shutdown_grace_seconds`;
- resets per-run graceful-stop state so the same `LocalSupervisor` instance can
  safely supervise sequential children;
- guarantees bounded child cleanup on unexpected wait/signal errors and on
  parent `KeyboardInterrupt` / `SystemExit`, without replacing an already-active
  primary exception;
- restores signal handlers after every path; restoration failures never replace an
  already-active primary exception, and raise `WorkerError` when no primary
  exists;
- wraps child-start `OSError` as `WorkerError` so the CLI returns exit code 2
  without a traceback.

On POSIX platforms the graceful stop request is `SIGTERM` via
`Popen.terminate()`. On Windows, `run_local.py` starts the worker in a new
process group and sends `CTRL_BREAK_EVENT` when available, falling back to
`terminate()` only when that signal is unavailable.

A later PR will add the localhost Streamlit child. No Streamlit process is
launched now.

## Timing validation

Worker timing settings are strict positive finite durations. `NaN`, positive or
negative infinity, booleans, zero, negative values, and values above the
documented maximum supported duration (`MAX_DURATION_SECONDS`, 30 days) are
rejected for polling, heartbeat, stale-job timeout, retry backoff, and shutdown
grace settings. Repository lease durations, stale thresholds, heartbeat
intervals, and wait/join timeouts use the same representable-duration rules so
`timedelta` and wait APIs cannot overflow.

Datetime arithmetic uses shared `add_duration` / `subtract_duration` helpers that
require timezone-aware inputs, validate the duration, and convert datetime-range
overflow or underflow into `RepositoryError` without clamping. Recovery batch
sizes are bounded by `MAX_RECOVERY_BATCH_SIZE` (`5000`) in configuration and in
`recover_expired_leases` before any SQLite `LIMIT` binding.

## Migration 0002 upgrade preflight

`0002_worker_runtime.sql` creates the worker runtime table, the unique
`uq_worker_instances_current_job` index, lease-recovery indexes, and running-job
lease triggers. After creating the triggers it runs a no-op `UPDATE jobs` over
existing rows so legacy v1 databases are validated against the new invariant.

If a legacy database contains a `running` job without both `lease_owner` and
`lease_expires_at`, the trigger aborts the migration. Because the migration SQL
and migration-history insert share one transaction, the upgrade rolls back
atomically and leaves the legacy rows unchanged for manual repair.

## Event detail payloads

`job_events.details_json` is a canonical JSON object. Infrastructure-owned keys
such as `worker_id`, `attempt`, `maximum_attempts`, `available_at`, and `error`
are reserved by the queue. Caller-supplied event metadata for failure/retry paths
must be nested under the reserved `details` key so future top-level fields can be
added without colliding with handler-defined payloads.

## Connection and transaction ownership

- connection context owns connection lifetime;
- transaction context owns commit/rollback;
- repositories own neither;
- public repository writes require an active explicit transaction;
- `RuntimeContext` never stores an open SQLite connection;
- claim, recovery, and finalization use `BEGIN IMMEDIATE`;
- no transaction remains open during handler execution, sleeping, or subprocess
  waiting.

## Current limitations

- one job at a time per worker process;
- no cooperative running-job cancellation;
- no sports-domain handlers;
- no Streamlit supervision yet;
- at-least-once, not exactly-once;
- handlers must eventually be idempotent when they perform side effects.


## Football ingestion handler

Registered job type: `ingest.football-data-csv`

Payload keys:

- required: `competition_id`, `season`
- optional: `raw_sha256`
- unknown keys rejected; arbitrary URL/path/division controls rejected

The handler checkpoints between major stages (before download/cache, after raw
acquisition, after parsing, after normalization, before publication, after
publication). Transient source/network and snapshot-busy errors map to
`RetryableJobError`. Malformed CSV, unsupported competition/season, HTTP 404/410,
integrity conflicts, and similar failures map to `PermanentJobError`.

Snapshot READY reuse makes identical source-content reprocessing idempotent at
the snapshot layer. No SQLite transaction remains open during download, CSV
parsing, or Parquet writing.
