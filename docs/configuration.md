# Configuration reference

This document describes the local configuration system for
`sports-analytics-local`.

## Sections

| Section | Purpose |
| --- | --- |
| `application` | Name, environment, timezone, deterministic seed |
| `storage` | Local filesystem locations for data and logs |
| `logging` | Console and optional rotating file logging |
| `worker` | Durable local worker timing, lease, retry, and shutdown settings |
| `scraping` | Settings for a future ingestion coordinator |
| `modelling` | Settings for future local modelling |

All models are immutable after validation and reject unknown fields.

## Built-in defaults

The application starts with valid built-in defaults that correspond closely to
`config/settings.example.toml`. A settings file is not required merely to start
a placeholder entry point. Default relative paths remain under `storage/`.

## Supported inputs and precedence

Sources, from lowest to highest precedence:

1. built-in model defaults
2. TOML configuration file
3. local `.env` file
4. operating-system environment variables
5. explicit programmatic overrides

Each `load_settings(...)` call is deterministic from its explicit inputs. The
loader does not mutate `os.environ`, does not cache settings globally, and does
not search parent directories for configuration files.

## TOML loading

- Default path: `config/settings.toml`
- If the default file is absent, built-in defaults continue.
- If an explicitly selected file is absent, loading fails with
  `ConfigurationError`.
- Invalid TOML fails with `ConfigurationError`.
- Invalid UTF-8 encoding fails with `ConfigurationError` (cause preserved).
- An empty valid TOML document means no overrides.
- Unknown sections or keys fail validation.

Use the standard-library `tomllib` module only.

## `.env` loading

- Default path: `.env`
- Absent default `.env` is ignored.
- An explicitly requested missing env file raises `ConfigurationError`.
- Loading uses `python-dotenv` (`dotenv_values`) and does not mutate process
  environment state.
- Parent-directory `.env` files are not loaded automatically.
- **Dotenv interpolation is intentionally disabled** (`interpolate=False`).
  Values containing `${NAME}` are treated as **literal** strings. This prevents
  hidden reads from the real process environment when callers pass an explicit
  `environ={...}` mapping. Operating-system overrides must use normal
  `SPORTS_ANALYTICS_` variables rather than relying on dotenv expansion.
- Invalid UTF-8 in an explicitly selected `.env` file raises
  `ConfigurationError` with the original decoding exception chained as
  `__cause__`.

## Environment variables

Namespace prefix: `SPORTS_ANALYTICS_`

Nested fields use a double underscore:

```text
SPORTS_ANALYTICS_APPLICATION__ENVIRONMENT=production
SPORTS_ANALYTICS_APPLICATION__TIMEZONE=Europe/Lisbon
SPORTS_ANALYTICS_LOGGING__LEVEL=DEBUG
SPORTS_ANALYTICS_WORKER__POLL_INTERVAL_SECONDS=10
SPORTS_ANALYTICS_WORKER__HEARTBEAT_INTERVAL_SECONDS=5
SPORTS_ANALYTICS_WORKER__STALE_JOB_TIMEOUT_SECONDS=60
SPORTS_ANALYTICS_WORKER__RETRY_BACKOFF_BASE_SECONDS=5
SPORTS_ANALYTICS_WORKER__RETRY_BACKOFF_MAX_SECONDS=300
SPORTS_ANALYTICS_WORKER__SHUTDOWN_GRACE_SECONDS=30
SPORTS_ANALYTICS_WORKER__RECOVERY_BATCH_SIZE=100
SPORTS_ANALYTICS_SCRAPING__ENABLED=false
```

Rules:

- Unrelated variables without the prefix are ignored.
- Unknown prefixed variables are rejected via strict model validation.
- Boolean and integer parsing uses Pydantic's typed parsing (for example
  `true` / `false`), not custom permissive converters.

### Control variable

`SPORTS_ANALYTICS_CONFIG_PATH` selects the TOML file. It is **not** part of the
validated settings model.

Config-file selection precedence (separate from value precedence):

1. explicit `config_path` / `--config`
2. `SPORTS_ANALYTICS_CONFIG_PATH` from the operating-system environment mapping
3. `SPORTS_ANALYTICS_CONFIG_PATH` from the `.env` file
4. default `config/settings.toml`

## Explicit programmatic overrides

Callers and tests may pass an `overrides` mapping to `load_settings` or
`bootstrap_runtime`. Overrides win over every other source. Input mappings are
never mutated.

## Relative path semantics

Relative storage paths are resolved against an explicit base directory supplied
to the loader / path resolver (normally the process working directory at
bootstrap time). Absolute paths remain absolute. Resolution does not change the
process working directory.

## Validation behaviour

Invalid values, unknown fields, malformed TOML, invalid UTF-8 encodings,
missing explicitly requested files, unsafe log file names, invalid timezones,
invalid log levels, invalid percent-style `logging.format` strings, and invalid
worker timing relationships all raise `ConfigurationError`. Human-facing messages
identify the failure and relevant file or field when known. Secrets and complete
environment dumps are never included.

Component identifiers are validated in **both** normal bootstrap and
`--validate-config` CLI modes. Invalid component names return exit code `2`
without creating directories or log files.

### Worker settings

The `worker` section controls durable queue polling, lease ownership, retry
backoff, and process shutdown:

| Field | Default | Meaning |
| --- | ---: | --- |
| `poll_interval_seconds` | `30` | Idle sleep between claim attempts when no job is available. |
| `heartbeat_interval_seconds` | `15` | Interval for idle worker heartbeats and running-job lease renewals. |
| `stale_job_timeout_seconds` | `300` | Lease duration and stale-worker threshold. |
| `retry_backoff_base_seconds` | `5` | First retry delay after a failed claimed attempt. |
| `retry_backoff_max_seconds` | `300` | Maximum deterministic retry delay. |
| `shutdown_grace_seconds` | `30` | Grace period for worker heartbeat cleanup and supervised child termination. |
| `recovery_batch_size` | `100` | Maximum expired running-job leases recovered in one pass. |

Validation relationships:

- all worker timing settings must be positive finite numbers and not booleans;
- `stale_job_timeout_seconds` must be greater than
  `heartbeat_interval_seconds`;
- `retry_backoff_max_seconds` must be greater than or equal to
  `retry_backoff_base_seconds`;
- `recovery_batch_size` must be a positive integer and not a boolean.

Retry scheduling is deterministic and has no jitter:

```text
min(max, base * 2**(attempts-1))
```

where `attempts` is the count after the job was claimed.

### Test isolation

Configuration and entry-point tests must isolate:

- process `SPORTS_ANALYTICS_*` variables;
- repository-local or developer `.env` files;
- filesystem side effects via `tmp_path` base directories.

Prefer explicit `environ={}` / scrubbed subprocess environments and temporary
base directories so built-in-default tests do not depend on developer machine
state. Explicit environment mappings are respected without hidden dotenv
interpolation.

## CLI usage

All five root entry points support mutually exclusive modes:

```bash
python engine.py --validate-config
python engine.py --database-status
python engine.py --migrate-database
python engine.py --config path/to/settings.toml
python engine.py --env-file path/to/.env
python worker.py --queue-status
python worker.py --recover-expired-leases
python worker.py --once
python run_local.py --worker-once
```

### Windows PowerShell

```powershell
python engine.py --validate-config
$env:SPORTS_ANALYTICS_LOGGING__LEVEL = "DEBUG"
python engine.py
```

### Linux and macOS

```bash
python engine.py --validate-config
SPORTS_ANALYTICS_LOGGING__LEVEL=DEBUG python engine.py
```

`--validate-config` validates and resolves configuration without creating
runtime directories, writing log files, configuring persistent handlers, seeding
global random state, or touching SQLite.

`--database-status` loads configuration, resolves `storage.sqlite_path`, and
inspects an existing database read-only. It does not create directories or apply
migrations. Missing or inconsistent databases return exit code `2`.

`--migrate-database` loads configuration, creates the SQLite parent directory if
needed, applies pending migrations, and exits without starting workers or
business workflows.

Normal non-worker execution bootstraps the runtime (directories, seeding,
logging, automatic idempotent SQLite migration) and then prints that the
component business functionality is not implemented. `worker.py` starts the
durable local worker, and `run_local.py` currently supervises only that worker
child.

Default SQLite path: `storage/operational.sqlite3` (`storage.sqlite_path`).

Exit codes:

- `0` — success
- `2` — expected configuration, database, or bootstrap error

## Logging

- Console logging always available on stderr
- Optional rotating file logging inside the resolved logs directory
- UTC timestamps
- Project-managed handlers are idempotent and closable for tests

Do not log complete settings objects, environment mappings, tokens, or
credentials.

## Deterministic seed

`application.deterministic_seed` seeds Python `random` and NumPy's legacy global
generator during normal bootstrap only.

## Security notes

- Do not store secrets in TOML or committed example files.
- Do not commit a real `.env`.
- Do not evaluate configuration as code.
- Do not load arbitrary Python configuration modules.
- Do not use pickle for configuration.
- Do not follow network URLs during configuration loading.

## Representative example

```toml
[application]
environment = "development"
timezone = "UTC"
deterministic_seed = 42

[worker]
poll_interval_seconds = 30
heartbeat_interval_seconds = 15
stale_job_timeout_seconds = 300
retry_backoff_base_seconds = 5
retry_backoff_max_seconds = 300
shutdown_grace_seconds = 30
recovery_batch_size = 100

[logging]
level = "INFO"
file_enabled = true
file_name = "sports-analytics.log"
```

```bash
SPORTS_ANALYTICS_APPLICATION__ENVIRONMENT=production
SPORTS_ANALYTICS_LOGGING__LEVEL=WARNING
SPORTS_ANALYTICS_WORKER__HEARTBEAT_INTERVAL_SECONDS=10
SPORTS_ANALYTICS_WORKER__STALE_JOB_TIMEOUT_SECONDS=120
```
