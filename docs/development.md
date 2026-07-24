# Development guide

Conventions for contributing to `sports-analytics-local`.

## Branch naming

- Do not commit directly to `main`.
- Prefer short, descriptive branch names.
- Cloud Agent / Cursor feature branches follow: `cursor/<descriptive-name>-<suffix>`.
- Use lowercase letters in branch names.

Examples:

- `cursor/repo-bootstrap-3ad9`
- `feat/sqlite-job-queue`
- `fix/parquet-snapshot-paths`

## Commit messages

Use conventional-style commit messages:

- `feat: ...` — user-visible capability
- `fix: ...` — bug fix
- `docs: ...` — documentation only
- `chore: ...` — tooling, packaging, housekeeping
- `test: ...` — tests only
- `refactor: ...` — internal restructuring without behaviour change

Keep the subject concise and imperative.

## Pull requests

- Keep pull requests focused on one concern.
- Include a clear summary of what changed and why.
- Note any intentional non-goals or follow-up work.
- Link related issues when applicable.
- Expect review before merge; do not merge your own bootstrap or experimental work without review.
- Use the pull-request template under `.github/pull_request_template.md`.

### Quality gates: Cursor vs GitHub

- The Cursor agent runs the **complete local quality suite** (install, pytest,
  ruff, mypy, and related checks).
- GitHub Actions provides an **additional clean Windows / Python 3.12
  compatibility check**. It does not redundantly re-run the Linux suite.
- The Windows check must finish successfully before merge.
- A successful Cursor Agent report does **not** replace code review or the
  Windows GitHub check.
- Failed checks must be investigated and fixed; do not bypass them.
- Branch protection may be configured manually after the quality workflow is
  active. Do not assume it is already enabled.

## Tests and validation

Before requesting review, run:

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
```

- Behaviour changes require tests.
- Do not add meaningless assertions (for example `assert True`).
- Prefer deterministic fixtures under `tests/fixtures/`.

### Isolating configuration and database tests

- Pass explicit `environ={...}` mappings to `load_settings` / bootstrap helpers
  instead of mutating the developer's real process environment when practical.
- Clear inherited `SPORTS_ANALYTICS_*` variables for entry-point and subprocess
  tests (see fixtures in `tests/conftest.py`).
- Use `tmp_path` as `base_directory` / cwd for TOML, `.env`, storage, and SQLite
  side effects. Do not write test artifacts into the repository `storage/` tree.
- Do not rely on a repository-local or developer `.env` file.
- Call `reset_logging()` (or equivalent cleanup) so rotating file handlers do not
  leave open handles, especially on Windows.
- Close every SQLite connection before asserting file deletion on Windows.
- Do not create a hidden global settings singleton or memoize loaded settings.
- Do not keep a module-level SQLite connection.

### Validating configuration and database locally

```bash
python engine.py --validate-config
python engine.py --config config/settings.example.toml --validate-config
python engine.py --database-status
python engine.py --migrate-database
```

Validation-only mode must not create runtime directories, log files, or SQLite
databases.

### Adding a migration

1. Create `src/sports_analytics/data/sql/migrations/NNNN_name.sql` with the next
   consecutive version.
2. Keep SQL free of transaction-control first tokens (`BEGIN`, `COMMIT`, `END`,
   `ROLLBACK`, `SAVEPOINT`, `RELEASE`), `VACUUM` / `ATTACH` / `DETACH`, and
   **all** `PRAGMA` statements. Connection PRAGMAs belong in `database.py`.
   Compound statements such as triggers with internal semicolons are supported by
   the splitter; trailing comment-only content is ignored.
3. Load migrations only through `importlib.resources` (package data), never via
   repository-relative filesystem paths in application code.
4. Never edit an already-applied migration after it has shipped. Checksums are
   immutable. In particular, `0001_initial.sql` must remain unchanged; its
   checksum is
   `404e1c0b36390ff7a42de901f344edcb60b9cee248b741116bc9d47a17cf48de`.
5. Add tests that exercise discovery from the installed package resource path.
6. When adding tests for `0002_worker_runtime.sql`, cover the
   `worker_instances` table, worker indexes, `idx_jobs_running_lease_expires`,
   and the running-job lease triggers for both insert and update paths.
7. Document the new version and checksum in the pull request.

### Transaction ownership

```text
with connect_database(path) as connection:
    with transaction(connection, immediate=True):
        repo = JobRepository(connection)
        ...
```

- connection context owns lifetime
- transaction context owns commit/rollback
- repositories own neither
- repository write tests must assert that writes outside `transaction(...)`
  raise `RepositoryError` and leave no rows (cover every public write method,
  including update/transition helpers after creating prerequisite rows)
- transaction tests should cover successful commit, caller-body rollback,
  `BEGIN IMMEDIATE`, nested rejection, and commit-time failures (for example
  deferred foreign-key violations) that must roll back and leave the connection
  reusable
- existing non-SQLite files (empty, short, partial header, arbitrary) must be
  rejected unchanged without WAL/SHM sidecars; nonexistent paths may create a DB
- do not use `Path.read_bytes()` when inspecting SQLite headers; read exactly
  the 16-byte header
- integer affinity alone is insufficient: validate with strict Python `int`
  checks and SQLite `typeof(...)` constraints
- migration-history corruption tests should assert `DatabaseMigrationError` at
  the library boundary and exit code `2` without traceback at the CLI
- migration parser tests should cover compound `CREATE TRIGGER` statements,
  trailing comments, comment-only files, block comments that must not concatenate
  tokens (for example `INT/* x */NOT NULL`), packaged resource read failures
  wrapped as `DatabaseMigrationError`, and migration definitions that are
  type-validated before sorting
- cleanup tests should assert that rollback/close failures do not replace an
  already-active caller or commit exception, while close failures after a
  successful body still propagate

### Durable worker handlers

Handlers implement the `JobHandler` protocol:

```python
def handler(context: JobExecutionContext, payload: JsonValue) -> JsonValue:
    context.checkpoint()
    ...
    return {"ok": True}
```

Guidelines:

- register handlers through a local `HandlerRegistry`; do not load executable
  code from job payloads, database rows, environment variables, arbitrary module
  paths, or user-entered import strings;
- keep handlers idempotent before they perform side effects. The queue is
  at-least-once, so a job can run again after crash, lease expiry, and recovery;
- call `context.checkpoint()` around meaningful work so cooperative shutdown and
  lease-loss checks are observed;
- raise `RetryableJobError` for transient failures and `PermanentJobError` for
  terminal validation/business failures;
- return canonical JSON-compatible data only;
- do not log payloads, result JSON, credentials, tokens, or raw external
  responses;
- treat `system.noop` as infrastructure-only test plumbing. Do not enqueue it as
  product work and do not build sports-domain behaviour on top of it.

Worker tests should inject clocks, sleepers, monotonic time, UUID factories, and
process metadata rather than relying on wall-clock sleeps or host-specific
values. Concurrency tests should exercise atomic claim ordering, lease fencing,
heartbeat renewal, expired-lease recovery, and finalization rejection for stale
owners. Prefer explicit `BEGIN IMMEDIATE` contention tests over timing-sensitive
thread sleeps when practical.

Review-regression tests for the worker/supervisor path should stay
deterministic and cover:

- heartbeat continuation during graceful shutdown while a handler is still
  running;
- post-checkpoint lease-loss races that must prevent success, retry, or failure
  finalization;
- occupied-worker claims where `worker_instances.current_job_id` is already set;
- Windows supervision using a new process group and `CTRL_BREAK_EVENT`;
- migration upgrade corruption cases, especially legacy v1 `running` jobs that
  lack a complete lease and must roll back atomically.

On Windows, subprocess tests must clean up children deterministically. Assert
that supervised worker children receive terminate first, that
`shutdown_grace_seconds` is respected, and that stubborn children are killed and
waited on before temporary files or SQLite databases are removed.

### Logging and secrets

- Log safe metadata only (component, environment, timezone, seed, base directory,
  whether file logging is enabled, schema version, database path).
- Never log complete environment mappings, full settings dumps, tokens,
  credentials, job payloads, result JSON, audit details wholesale, or arbitrary
  SQL parameters.

## Prohibitions

- Do **not** commit directly to `main`.
- Do **not** commit generated data (Parquet, SQLite, model artifacts, exports, logs).
- Do **not** commit credentials, tokens, API keys, or secrets.
- Do **not** add paid API dependencies without an explicit architecture decision.
- Do **not** add external AI / LLM runtime dependencies.
- Do **not** commit virtual environments or tool caches.

## Code expectations

- Keep code typed (annotations throughout).
- Prefer deterministic behaviour and explicit failure modes.
- Keep changes auditable and reviewable.
- Avoid speculative interfaces and unused abstractions in early milestones.

## Recommended review outcomes

Reviewers should conclude with one of:

- **MERGE** — ready to merge as-is (or with trivial nits).
- **CHANGES REQUIRED** — must address specific feedback before merge.
- **DO NOT MERGE** — design or scope issues block the change.

## Local quality hooks

Install pre-commit once per clone:

```bash
pre-commit install
```

Hooks enforce whitespace, end-of-file, YAML/TOML validity, private-key detection, Ruff lint (with safe fixes), and Ruff formatting.
