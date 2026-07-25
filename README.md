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

This repository is in **pre-alpha** state.

Implemented now:

- packaging, typed configuration, local runtime bootstrap, and logging;
- SQLite operational persistence with forward-only migrations `0001`, `0002`, and
  `0003`;
- durable local job worker infrastructure and a worker-only supervisor;
- one Football-Data.co.uk **ingestion adapter** covering two competitions
  (`eng-premier-league`, `prt-primeira-liga`), with allowlisted HTTPS retrieval,
  content-addressed raw storage, and strict CSV parsing;
- sport-agnostic canonical participant and event contracts with source
  participant/event provenance datasets, plus conservative versioned participant
  and event reconciliation;
- a generic canonical market quote contract, with historical 1X2 mapped into it;
- immutable generic Parquet snapshots;
- worker job integration: the `ingest.football-data-csv` handler is registered in
  the frozen default registry;
- snapshot listing and verification through `scraper.py`;
- documentation, linting, typing, and tests.

**Not implemented**: Betclic; Betano; current bookmaker prices; browser scraping
or automation; additional sports; markets beyond production 1X2 plus a synthetic
contract proof; models; features; predictions; combinations and accumulators;
backtesting; settlement; bankroll management; Streamlit UI pages; an opportunity
search engine; an automatic bet builder; user bet filters; cross-source fuzzy
resolution.

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
operational metadata in SQLite:

```text
storage/snapshots/football-ingestion/football-canonical-v2/<competition_id>/<YYYY-YYYY>/<snapshot-uuid>/
```

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
supervision is planned for a later phase. The frozen default handler registry
contains `system.noop` (infrastructure only) and `ingest.football-data-csv`
(football ingestion).

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

`--list-sources` prints one tab-separated line per implemented source adapter
(`source_id`, `display_name`, role, adapter version, capabilities, supported
sports) without touching the database or the network. Only the
Football-Data.co.uk ingestion adapter is registered; no bookmaker or current-odds
adapter exists.

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

| File | Role | Status |
| --- | --- | --- |
| `app.py` | Streamlit user interface entry point | Placeholder |
| `scraper.py` | Coordinates permitted public data ingestion | Implemented |
| `engine.py` | Coordinates features, predictions, and combinations | Placeholder |
| `worker.py` | Runs durable background jobs outside the Streamlit process | Implemented |
| `run_local.py` | Supervises the local worker child process | Implemented |

Each file supports shared validation / database CLI modes. `worker.py` and
`run_local.py` implement durable worker infrastructure. `scraper.py` implements
source and competition listing, football ingestion enqueue, snapshot listing, and
read-only snapshot verification. `app.py` and `engine.py` remain
business-function placeholders after bootstrap.

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
│       │   └── sql/migrations/
│       │       ├── 0001_initial.sql
│       │       ├── 0002_worker_runtime.sql
│       │       └── 0003_snapshot_source_deduplication.sql
│       ├── jobs/
│       ├── local/
│       ├── sources/
│       │   └── football_data_co_uk/
│       ├── sports/
│       │   └── football/
│       ├── markets/
│       ├── snapshots/
│       ├── ingestion/
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
    ├── data-contracts.md
    ├── database.md
    ├── development.md
    ├── snapshots.md
    ├── sources.md
    └── worker.md
```

`scrapers`, `features`, `models`, `combinations`, `services`, and `evaluation` are
reserved package placeholders and are currently empty.

## High-level architecture

- **Streamlit** (`app.py`) for local interactive use (planned).
- **Ingestion coordinator** (`scraper.py`) for permitted public sources only
  (implemented for the Football-Data.co.uk ingestion adapter).
- **Engine** (`engine.py`) for deterministic feature generation, local models, and
  combination proposals (planned).
- **Worker** (`worker.py`) for durable background jobs, leases, and retries
  (implemented).
- **SQLite** for operational state, jobs, snapshot metadata, and audit records.
- **Parquet** for historical and analytical datasets under versioned immutable
  snapshots.

See [docs/architecture.md](docs/architecture.md) for principles and boundaries.

## Current limitations

- Only one source is implemented: the Football-Data.co.uk historical CSV
  **ingestion adapter**. No **bookmaker / current-odds adapter** exists, so there
  is no Betclic, no Betano, and no current price data.
- Only two football competitions are supported, and only historical 1X2 is
  emitted into the generic market contract. A synthetic totals fixture proves the
  contract generalizes, but no adapter produces other markets.
- Cross-source resolution is limited to exact canonical identity. There is no
  fuzzy or machine-learning matching and no silent alias merge; unresolved source
  events stay in `source_events` and are excluded from downstream-safe datasets.
- No feature engineering, models, predictions, combinations, accumulators,
  backtesting, settlement, or bankroll management.
- No opportunity search engine, automatic bet builder, or user bet filters.
- No sports-domain SQLite schemas: analytical data lives in Parquet snapshots and
  SQLite stores only snapshot metadata.
- No browser automation and no HTML scraping.
- `run_local.py` supervises only the worker; no Streamlit child process is
  started yet, and no Streamlit UI pages exist.
- `system.noop` remains infrastructure-only; `ingest.football-data-csv` is the
  only sports-domain job handler.
- `app.py` and `engine.py` remain functional placeholders after bootstrap.

## Contribution workflow

1. Create a focused feature branch (do not commit directly to `main`).
2. Make typed, deterministic, auditable changes.
3. Add or update tests for behaviour you change.
4. Run the validation commands above.
5. Open a pull request for review and wait for the Windows GitHub check.

See [docs/development.md](docs/development.md) for conventions and review outcomes.

## Disclaimer

This software is for **analytical and educational purposes** only. It does **not** guarantee correct predictions. It does **not** guarantee profitable betting outcomes. Users remain responsible for complying with applicable laws and platform terms. Responsible gambling principles should be followed.
