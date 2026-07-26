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
- SQLite operational persistence with forward-only migrations `0001`–`0004`;
- durable local job worker infrastructure and a worker-only supervisor;
- one Football-Data.co.uk **ingestion adapter** covering two competitions
  (`eng-premier-league`, `prt-primeira-liga`), with allowlisted HTTPS retrieval,
  content-addressed raw storage, and strict CSV parsing;
- sport-agnostic canonical participant and event contracts with source
  participant/event provenance datasets, plus conservative versioned participant
  and event reconciliation;
- a generic canonical market quote contract, with historical 1X2 mapped into it;
- immutable generic Parquet snapshots;
- worker job integration for `ingest.football-data-csv`,
  `settlement.settle-analysis`, and `monitoring.run` in the frozen registry;
- snapshot listing and verification through `scraper.py`;
- leakage-safe football full-match 1X2 feature engineering
  (`football-1x2-prematch-features-v1`);
- deterministic rolling-origin training, temperature calibration, evaluation, and
  pickle-free model artifacts (`football-1x2-logistic-v1`) via `engine.py`;
- generic immutable predictions, complete-market implied probability/edge/EV,
  auditable opportunity filtering, bounded dependency-safe combinations, pure
  flat-unit settlement, and rolling-origin backtesting contracts;
- a Football-Data market-average closing-price **historical benchmark** for
  football 1X2 singles, with content-addressed analytical artifacts;
- a read-only Streamlit interface for verified analysis/backtest artifact
  selection, data status, opportunity browsing, single audit, dependency-safe
  manual previews, persisted combinations, and backtest/audit views;
- canonical result snapshots, deterministic analytical settlement,
  persisted-evidence monitoring, and explicit champion–challenger governance;
- documentation, linting, typing, and tests.

**Not implemented**: Betclic; Betano; current bookmaker prices; browser scraping
or automation; additional sports; markets beyond production 1X2 plus a synthetic
contract proof; player/lineup/injury features; Kelly staking; bet
recommendations; production accumulators; real bookmaker settlement; bankroll
management; live automatic bet building; cross-source fuzzy resolution.

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

### Football 1X2 modelling workflow

After one or more READY football snapshots exist for a single competition:

```bash
python engine.py --config config/settings.toml --build-football-1x2-features \
  --snapshot football-ingestion/football-canonical-v2/<competition>/<season>/<uuid>/manifest.json \
  --snapshot football-ingestion/football-canonical-v2/<competition>/<season2>/<uuid>/manifest.json

python engine.py --config config/settings.toml --train-football-1x2 \
  --features football/football-1x2-prematch-features-v1/<competition>/<artifact-id>

python engine.py --config config/settings.toml --verify-model \
  football/football-1x2-logistic-v1/<competition>/<artifact-id>/model.json
```

This baseline is team-level historical football 1X2 only. It is not a betting
recommendation engine, does not use players/injuries/lineups, and does not use
bookmaker odds as model features. See [docs/modelling.md](docs/modelling.md).

### Football 1X2 closing-line benchmark

```bash
python engine.py --config config/settings.toml --backtest-football-1x2 \
  --features football/football-1x2-prematch-features-v1/<competition>/<artifact-id>
```

This is a rolling-origin singles benchmark against Football-Data market-average
closing prices. It does not claim those prices were available before kickoff and
explicitly refuses production closing-line accumulators. See
[docs/prediction-value-backtesting.md](docs/prediction-value-backtesting.md).

Focused generic workflows use explicit JSON files (never “latest” discovery):

```bash
python engine.py --generate-predictions prediction-request.json
python engine.py --evaluate-opportunities evaluation-request.json
python engine.py --build-combinations combination-request.json
python engine.py --validate-combination manual-combination.json
python engine.py --run-backtest backtest-request.json

python engine.py --config config/settings.toml --verify-backtest-artifact \
  <relative-directory> --artifact-schema football-1x2-closing-backtest-v1
```

Published backtests are typed, content-addressed multi-dataset artifacts with
strict manifest, JSONL schema/count/hash, lineage, timing, and duplicate checks.

### Streamlit artifact interface

Run the application from the repository root with the Python 3.12 virtual
environment:

```powershell
.venv\Scripts\python.exe -m streamlit run app.py
```

The sidebar catalogue discovers supported typed `analysis` and `backtest`
artifacts below the configured `storage.exports_directory`. A candidate is
selectable only after the existing typed artifact loader verifies its manifest,
checksum sidecar, dataset checksums, artifact identity, row schemas, canonical
ordering, and cross-dataset links. Invalid candidates remain visible as
validation issues and are never exposed as trusted data.

The interface is read-only over persisted analytical artifacts. It provides data
status, opportunity filters and single-detail audit, local manual accumulator
validation through the existing dependency/combination domain layer, persisted
combination browsing, persisted backtest metrics/charts, and raw views of already
validated typed rows. It does not scrape, ingest, train, predict, write
artifacts, mutate datasets, or write to SQLite.

Persisted provenance remains explicit:

- `synthetic-contract` rows are contract/test data;
- `historical-replay` rows are historical analytical replays;
- closing-line historical benchmarks are not executable bookmaker offers;
- future live data would require providers and workflows that do not exist yet.

There is no Betclic, Betano, live/upcoming odds feed, staking, bookmaker
submission, or operational settlement in the interface. Manual accumulator
results are local interactive previews only and are not persisted or represented
as accepted bets.

The UI uses a small CSS-only decorative theme with three low-opacity blurred
orbs, slow transform/opacity motion, visible focus states, and native Streamlit
controls. Decorative motion communicates no analytical meaning and is disabled
under the operating system's `prefers-reduced-motion` setting.

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
| `app.py` | Read-only Streamlit analytical artifact interface | Implemented |
| `scraper.py` | Coordinates permitted public data ingestion | Implemented |
| `engine.py` | Coordinates features, models, predictions, value, and backtesting | Implemented |
| `worker.py` | Runs durable background jobs outside the Streamlit process | Implemented |
| `run_local.py` | Supervises the local worker child process | Implemented |

Each file supports shared validation / database CLI modes. `worker.py` and
`run_local.py` implement durable worker infrastructure. `scraper.py` implements
source and competition listing, football ingestion enqueue, snapshot listing, and
read-only snapshot verification. `app.py` renders the verified artifact
interface when launched through Streamlit and retains shared configuration and
database CLI compatibility when invoked as a regular Python script.

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
    ├── prediction-value-backtesting.md
    ├── snapshots.md
    ├── sources.md
    └── worker.md
```

The prediction, value, opportunities, combinations, backtesting, features,
models, services, and evaluation packages contain the deterministic analytics
pipeline. `scrapers` remains reserved.

## High-level architecture

- **Streamlit** (`app.py`) for local read-only artifact review (implemented).
- **Ingestion coordinator** (`scraper.py`) for permitted public sources only
  (implemented for the Football-Data.co.uk ingestion adapter).
- **Engine** (`engine.py`) for deterministic features, local models, prediction
  contracts, value evaluation, and historical backtesting (implemented).
- **Worker** (`worker.py`) for durable background jobs, leases, and retries
  (implemented).
- **SQLite** for operational state, jobs, snapshot metadata, and audit records.
- **Parquet** for historical and analytical datasets under versioned immutable
  snapshots.

See [docs/architecture.md](docs/architecture.md) for principles and boundaries.
See
[docs/settlement-monitoring-governance.md](docs/settlement-monitoring-governance.md)
for canonical result evidence, analytical settlement, monitoring, and explicit
champion–challenger operations.

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
- No current-price production opportunity feed, bookmaker settlement, staking,
  correlation model, or bankroll management. Deterministic flat-unit analytical
  settlement of verified persisted positions is implemented.
- Analytical datasets remain immutable artifacts. SQLite additionally stores
  minimal operational result, settlement, monitoring, and model-role indexes.
- No browser automation and no HTML scraping.
- `run_local.py` supervises only the worker; it does not start the implemented
  Streamlit interface, which is launched explicitly.
- `system.noop` remains infrastructure-only. Durable handlers include ingestion,
  deterministic analytical settlement, and persisted-evidence monitoring.
- `app.py` exposes a read-only interface over verified typed analytical
  artifacts.

## Contribution workflow

1. Create a focused feature branch (do not commit directly to `main`).
2. Make typed, deterministic, auditable changes.
3. Add or update tests for behaviour you change.
4. Run the validation commands above.
5. Open a pull request for review and wait for the Windows GitHub check.

See [docs/development.md](docs/development.md) for conventions and review outcomes.

## Disclaimer

This software is for **analytical and educational purposes** only. It does **not** guarantee correct predictions. It does **not** guarantee profitable betting outcomes. Users remain responsible for complying with applicable laws and platform terms. Responsible gambling principles should be followed.
