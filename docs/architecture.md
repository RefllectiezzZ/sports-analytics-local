# Architecture

This document describes the architecture for `sports-analytics-local`.

The repository currently provides packaging, typed configuration, local runtime
bootstrap, placeholders, documentation, and quality tooling. Sports analytics
business logic is **not** implemented yet.

## Entry points

| Component | Role |
| --- | --- |
| `app.py` | Streamlit entry point for local interactive analytics and review. |
| `scraper.py` | Coordinates data ingestion from permitted public sources. |
| `engine.py` | Coordinates feature generation, prediction, and combination generation. |
| `worker.py` | Executes background jobs outside the Streamlit process. |
| `run_local.py` | Coordinates local startup of the processes needed for localhost operation. |

Each root script now bootstraps a shared local runtime (or validates
configuration via `--validate-config`) and then reports that its business
functionality is not implemented.

## Configuration boundary

- Immutable Pydantic v2 models in `sports_analytics.core.settings` define the
  typed configuration surface (`Settings` and nested sections).
- Configuration is loaded deterministically from layered sources. Precedence
  (lowest to highest): built-in defaults, TOML file, `.env` file, operating-system
  environment variables, explicit programmatic overrides.
- Unknown sections and fields are rejected. Invalid values raise
  `ConfigurationError` with concise, actionable messages.
- There is no module-level settings singleton and no implicit memoization.

See [configuration.md](configuration.md) for the full configuration reference.

## Path resolution and directories

- Pure path resolution (`resolve_paths`) converts configured paths into absolute
  `RuntimePaths` against an explicit base directory.
- Directory creation (`create_runtime_directories`) is a separate side effect and
  is idempotent. It never creates the SQLite database file.
- Relative paths resolve against the supplied base directory, not against an
  imported module location. Absolute paths remain absolute.

## Runtime context

`bootstrap_runtime` loads settings, resolves paths, creates runtime directories,
seeds deterministic generators, configures logging, and returns an immutable
`RuntimeContext` containing the component name, settings, paths, UTC startup
timestamp, and component logger.

`--validate-config` uses `validate_configuration`, which loads and resolves
settings without creating directories, writing log files, configuring persistent
handlers, or seeding global random state.

## Logging boundary

- Standard-library logging only, under the `sports_analytics` namespace.
- Console logging always goes to stderr.
- Optional rotating file logging writes inside the resolved logs directory.
- Timestamps are UTC. Project-managed handlers are marked and replaced
  idempotently without calling `logging.basicConfig`.

## Deterministic seeding

Runtime bootstrap seeds Python `random` and NumPy's legacy global generator from
`application.deterministic_seed`. Future code should prefer explicitly passed
generators where practical. Hash randomization / `PYTHONHASHSEED` is not mutated
after interpreter startup.

## Storage principles (planned)

- **SQLite** will store operational state, metadata, jobs, predictions, and audit records.
- **Parquet** will store historical and analytical datasets.
- **Snapshots** will be immutable once written.
- Runtime-generated files live under `storage/`; only `.gitkeep` markers are tracked in Git.

## Processing principles (planned)

- Processing must be **deterministic**, including seeded modelling where applicable.
- Features, rules, datasets, and models will be **versioned**.
- Sports models and market models will eventually be **separated**.
- Failures must be **explicit and auditable** (no silent data loss or silent retries without records).

## External dependency boundaries

- External AI, LLM, or cloud inference services will **not** be runtime dependencies.
- Paid APIs will **not** be required for runtime operation.
- Scraping will use **permitted public sources only**.

## Package layout

Application code lives under `src/sports_analytics/` with focused subpackages:

- `core` — configuration, paths, logging, runtime bootstrap, shared CLI
- `data` — persistence and dataset I/O
- `scrapers` — ingestion adapters
- `features` — feature engineering
- `models` — local statistical / ML components
- `combinations` — combination generation helpers
- `services` — workflow orchestration
- `evaluation` — evaluation utilities

## Current implementation status

Implemented:

- typed configuration loading and validation;
- path resolution and safe runtime directory creation;
- local logging configuration;
- deterministic seeding;
- shared runtime bootstrap and CLI options;
- placeholder entry points wired to that bootstrap.

Explicitly **not** implemented yet:

- SQLite schemas and connections;
- worker job execution loops;
- scraping adapters and HTTP clients;
- modelling, feature engineering, predictions, and betting logic;
- Streamlit UI components;
- process spawning in `run_local.py`.
