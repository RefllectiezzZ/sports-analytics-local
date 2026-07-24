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

This repository is in **bootstrap / pre-alpha** state. Packaging, documentation, linting, typing, and a smoke test are in place. Application features (scraping, modelling, predictions, Streamlit UI, workers, and database schemas) are **not implemented**.

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

Each file is currently a typed placeholder that prints a not-implemented message.

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
- Configuration examples exist, but configuration loading is not implemented.
- Entry points are placeholders only.

## Contribution workflow

1. Create a focused feature branch (do not commit directly to `main`).
2. Make typed, deterministic, auditable changes.
3. Add or update tests for behaviour you change.
4. Run the validation commands above.
5. Open a pull request for review.

See [docs/development.md](docs/development.md) for conventions and review outcomes.

## Disclaimer

This software is for **analytical and educational purposes** only. It does **not** guarantee correct predictions. It does **not** guarantee profitable betting outcomes. Users remain responsible for complying with applicable laws and platform terms. Responsible gambling principles should be followed.
