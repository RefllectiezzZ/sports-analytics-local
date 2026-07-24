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
runtime bootstrap, documentation, linting, typing, and tests are in place.
Application features (scraping, modelling, predictions, Streamlit UI, workers,
and database schemas) are **not implemented**.

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

`SPORTS_ANALYTICS_CONFIG_PATH` selects the TOML file and is not part of the
validated settings model.

All five root scripts accept:

| Option | Meaning |
| --- | --- |
| `--config PATH` | Explicit TOML configuration file |
| `--env-file PATH` | Explicit dotenv file |
| `--validate-config` | Validate and resolve configuration only |

Normal bootstrap creates configured runtime directories and may create a rotating
log file when file logging is enabled. `--validate-config` does **not** create
directories or log files and does not seed global random state.

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

### Windows PowerShell overrides

```powershell
$env:SPORTS_ANALYTICS_LOGGING__LEVEL = "DEBUG"
python engine.py --validate-config
```

### Linux and macOS overrides

```bash
SPORTS_ANALYTICS_LOGGING__LEVEL=DEBUG python engine.py --validate-config
```

See [docs/configuration.md](docs/configuration.md) for the full reference.

## Validation commands

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

Pull requests and pushes to `main` are automatically checked on **Ubuntu** and **Windows** with **Python 3.12**. Automated checks include:

- dependency consistency (`pip check`);
- pytest;
- Ruff lint;
- Ruff format verification;
- mypy.

Local validation remains required before opening a pull request. GitHub-hosted CI validates the same quality gates on both platforms; this repository does not claim broader Windows application testing beyond those checks.

## Pre-commit

```bash
pre-commit install
pre-commit run --all-files
```

## Entry-point placeholders

| File | Future role |
| --- | --- |
| `app.py` | Streamlit user interface entry point |
| `scraper.py` | Coordinates permitted public data ingestion |
| `engine.py` | Coordinates features, predictions, and combinations |
| `worker.py` | Runs background jobs outside the Streamlit process |
| `run_local.py` | Coordinates local multi-process startup |

Each file bootstraps the shared local runtime (or validates configuration) and
then reports that its business functionality is not implemented.

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
    └── development.md
```

## High-level future architecture

- **Streamlit** (`app.py`) for local interactive use.
- **Scraper coordinator** (`scraper.py`) for permitted public sources only.
- **Engine** (`engine.py`) for deterministic feature generation, local models, and combination proposals.
- **Worker** (`worker.py`) for durable background jobs and retries.
- **SQLite** for operational state, jobs, predictions, and audit records.
- **Parquet** for historical and analytical datasets under versioned snapshots.

See [docs/architecture.md](docs/architecture.md) for principles and boundaries.

## Current limitations

- No scraping, prediction, ML, betting, or UI logic is implemented.
- No database schemas or tables are defined.
- No background job processing is implemented.
- Configuration and runtime bootstrap are implemented; analytics features are not.
- Entry points remain functional placeholders after bootstrap.

## Contribution workflow

1. Create a focused feature branch (do not commit directly to `main`).
2. Make typed, deterministic, auditable changes.
3. Add or update tests for behaviour you change.
4. Run the validation commands above.
5. Open a pull request for review.

See [docs/development.md](docs/development.md) for conventions and review outcomes.

## Disclaimer

This software is for **analytical and educational purposes** only. It does **not** guarantee correct predictions. It does **not** guarantee profitable betting outcomes. Users remain responsible for complying with applicable laws and platform terms. Responsible gambling principles should be followed.
