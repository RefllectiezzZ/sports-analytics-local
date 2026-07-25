# sports-analytics-local

Fully local, deterministic, fault-tolerant sports analytics and betting-support application.

## Objective

Provide a localhost-only toolchain that:

- collects permitted public sports data;
- stores operational state in SQLite;
- stores analytical and historical datasets in Parquet;
- trains local statistical and machine-learning models;
- generates auditable sports predictions and betting combinations.

## Runtime constraints

- Runtime operation does **not** depend on paid APIs.
- Runtime operation does **not** depend on external AI, LLM, or cloud inference services.
- Development tooling may use AI assistance, but the final application will not require it.

## Current status

This repository is in **pre-alpha** state. Packaging, typed configuration, local
runtime bootstrap, SQLite operational persistence, migrations, durable worker
infrastructure, Football-Data.co.uk CSV ingestion into immutable Parquet
snapshots, documentation, linting, typing, and tests are in place. Modelling,
predictions, recommendations, combinations, settlement, bankroll management, and
Streamlit UI pages are **not implemented**.

## Supported Python version

Python **3.12** or newer is required.

## Prerequisites

- Python 3.12+
- `pip` and the standard library `venv` module
- Git

## Setup

Create a virtual environment and install the project with development dependencies.

### Windows (PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

### Linux and macOS

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Optional environment and configuration examples:

```bash
cp .env.example .env
cp config/settings.example.toml config/settings.toml
```

## Configuration and runtime

The application has valid **built-in defaults**. A settings file is optional for
placeholder entry points.

Supported sources, lowest to highest precedence:

1. built-in defaults
2. optional `config/settings.toml` (or another TOML path)
3. optional `.env`
4. operating-system environment variables (`SPORTS_ANALYTICS_…`)
5. explicit programmatic overrides

Nested environment syntax uses a double underscore:

```text
SPORTS_ANALYTICS_APPLICATION__ENVIRONMENT=production
SPORTS_ANALYTICS_LOGGING__LEVEL=DEBUG
```

`.env` values are loaded literally (dotenv interpolation is disabled), so
`${NAME}` is not expanded from the process environment. Use `SPORTS_ANALYTICS_`
variables for operating-system overrides.

`SPORTS_ANALYTICS_CONFIG_PATH` selects the TOML file and is not part of the
validated settings model.

All five root scripts accept mutually exclusive modes:

| Option | Meaning |
| --- | --- |
| `--config PATH` | Explicit TOML configuration file |
| `--env-file PATH` | Explicit dotenv file |
| `--validate-config` | Validate and resolve configuration only |
| `--database-status` | Read-only inspect an existing SQLite database |
| `--migrate-database` | Apply pending SQLite migrations |

Normal bootstrap creates configured runtime directories, may create a rotating
log file when file logging is enabled, and ensures the configured SQLite database
is migrated and ready. `--validate-config` does **not** create directories, log
files, or SQLite databases and does not seed global random state.

Default SQLite location (configurable via `storage.sqlite_path`):

```text
storage/operational.sqlite3
```

No sports-domain tables exist in SQLite. Analytical football datasets are stored
as immutable Parquet snapshots under the configured snapshots directory, with
operational metadata in SQLite.

Logging:

- console logging always writes to stderr under the `sports_analytics` namespace;
- optional rotating file logging writes inside the resolved logs directory;
- timestamps are UTC.

Deterministic seeding:

- normal bootstrap seeds Python `random` and NumPy's legacy global generator from
  `application.deterministic_seed`.

### Validate configuration

```bash
python engine.py --validate-config
```

### Database status and migration

```bash
python engine.py --database-status
python engine.py --migrate-database
python engine.py --database-status
```

The first `--database-status` call fails when the database is missing and creates
nothing. `--migrate-database` creates the parent directory if needed and applies
pending migrations. The final `--database-status` call succeeds when the database
is valid and up to date.

### Worker and local supervisor

```bash
python worker.py --queue-status
python worker.py --recover-expired-leases
python worker.py --once
python worker.py
python run_local.py
python run_local.py --worker-once
```

`worker.py` runs the durable local job worker. `--queue-status` is read-only,
`--recover-expired-leases` runs one recovery batch, `--once` claims at most one
currently available job, and no flag starts the polling loop.

`run_local.py` currently supervises **only** the worker child process. Streamlit
supervision is planned for a later phase. Built-in handlers include `system.noop`
and `ingest.football-data-csv`.

### Football ingestion workflow

Enable scraping in a local config (do not commit secrets or operational data):

```toml
[scraping]
enabled = true
```

Then:

```bash
python scraper.py --config config/settings.toml --migrate-database
python scraper.py --config config/settings.toml --list-competitions
python scraper.py \
  --config config/settings.toml \
  --enqueue-football-data \
  --competition eng-premier-league \
  --season 2023-2024
python worker.py --config config/settings.toml --once
python scraper.py --config config/settings.toml --list-snapshots
python scraper.py --config config/settings.toml --verify-snapshot <SNAPSHOT_UUID>
```

The scraper enqueues a job; the worker performs the HTTPS download, raw storage,
normalization, and Parquet publication. Real downloads depend on the external
Football-Data.co.uk website and network availability. Users must review and
respect the source’s current terms.

Additional scraper modes:

```bash
python scraper.py --list-sources
python scraper.py --list-competitions
```

See [docs/sources.md](docs/sources.md), [docs/snapshots.md](docs/snapshots.md),
and [docs/data-contracts.md](docs/data-contracts.md).

### Windows PowerShell overrides

```powershell
$env:SPORTS_ANALYTICS_LOGGING__LEVEL = "DEBUG"
python engine.py --validate-config
```

### Linux and macOS overrides

```bash
SPORTS_ANALYTICS_LOGGING__LEVEL=DEBUG python engine.py --validate-config
```

See [docs/configuration.md](docs/configuration.md) and
[docs/database.md](docs/database.md) for the full references.

## Validation commands

The Cursor agent runs the complete local quality suite:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
```

Format sources (when needed):

```bash
python -m ruff format .
```

## Continuous integration

GitHub Actions provides an **additional clean Windows / Python 3.12 compatibility
check**. It does **not** redundantly re-run the same Linux suite already executed
by the Cursor agent.

- The Windows check must finish successfully before merge.
- A Cursor report does **not** replace code review.
- Ubuntu CI is not used for this repository's GitHub workflow.

Automated Windows checks include:

- dependency consistency (`pip check`);
- pytest;
- Ruff lint;
- Ruff format verification;
- mypy.

## Pre-commit

```bash
pre-commit install
pre-commit run --all-files
```

## Entry points

| File | Future role |
| --- | --- |
| `app.py` | Streamlit user interface entry point |
| `scraper.py` | Coordinates permitted public data ingestion |
| `engine.py` | Coordinates features, predictions, and combinations |
| `worker.py` | Runs durable background jobs outside the Streamlit process |
| `run_local.py` | Supervises the local worker child process |

Each file supports shared validation / database CLI modes. `worker.py` and
`run_local.py` implement durable worker infrastructure; `app.py`, `scraper.py`,
and `engine.py` remain business-function placeholders after bootstrap.

## Directory structure

```text
├── app.py
├── scraper.py
├── engine.py
├── worker.py
├── run_local.py
├── pyproject.toml
├── README.md
├── .gitignore
├── .env.example
├── .pre-commit-config.yaml
├── .github/
│   ├── pull_request_template.md
│   └── workflows/
│       └── quality.yml
├── config/
│   └── settings.example.toml
├── src/
│   └── sports_analytics/
│       ├── __init__.py
│       ├── core/
│       ├── data/
│       ├── scrapers/
│       ├── features/
│       ├── models/
│       ├── combinations/
│       ├── services/
│       └── evaluation/
├── storage/
│   ├── raw/
│   ├── snapshots/
│   ├── features/
│   ├── models/
│   ├── exports/
│   └── logs/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
└── docs/
    ├── architecture.md
    ├── configuration.md
    ├── database.md
    ├── development.md
    └── worker.md
```

## High-level future architecture

- **Streamlit** (`app.py`) for local interactive use.
- **Scraper coordinator** (`scraper.py`) for permitted public sources only.
- **Engine** (`engine.py`) for deterministic feature generation, local models, and combination proposals.
- **Worker** (`worker.py`) for durable background jobs, leases, and retries.
- **SQLite** for operational state, jobs, snapshot metadata, and audit records.
- **Parquet** for historical and analytical datasets under versioned snapshots.

See [docs/architecture.md](docs/architecture.md) for principles and boundaries.

## Current limitations

- No scraping, prediction, ML, betting, or UI logic is implemented.
- No sports-domain database schemas are defined.
- Durable worker claiming, lease heartbeat, expired-lease recovery, and
  `run_local.py` worker supervision are implemented.
- `run_local.py` supervises only the worker; no Streamlit child process is
  started yet.
- `system.noop` is infrastructure-only; no sports-domain jobs exist yet.
- Configuration, runtime bootstrap, and operational SQLite persistence are implemented.
- Non-worker business entry points remain functional placeholders after bootstrap.

## Contribution workflow

1. Create a focused feature branch (do not commit directly to `main`).
2. Make typed, deterministic, auditable changes.
3. Add or update tests for behaviour you change.
4. Run the validation commands above.
5. Open a pull request for review and wait for the Windows GitHub check.

See [docs/development.md](docs/development.md) for conventions and review outcomes.

## Disclaimer

This software is for **analytical and educational purposes** only. It does **not** guarantee correct predictions. It does **not** guarantee profitable betting outcomes. Users remain responsible for complying with applicable laws and platform terms. Responsible gambling principles should be followed.
