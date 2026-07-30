# sports-analytics-local

Fully local, deterministic, fault-tolerant sports analytics and betting-support application.

## Objective

Provide a localhost-only toolchain that:

- collects permitted public sports data;
- stores operational state in SQLite;
- stores analytical and historical datasets in Parquet;
- trains local statistical and machine-learning models;
- generates auditable sports predictions and betting combinations.

## Primary v1 football product path

The current product path is historical modelling and strict offline price
input, not direct bookmaker acquisition:

```text
historical evidence -> rolling tournament -> coherent joint score distribution
-> multi-market probabilities -> fair odds -> optional real offered-price input
-> EV/opportunities -> proposed singles/accumulators -> persisted Streamlit state
```

Independent dynamic Poisson and Dixon–Coles candidates derive the supported
full-time goal markets from one bounded score matrix. Fair odds are model
estimates; offered odds are real external prices. Without offered odds, EV and
proposed price-based bets do not exist.

Direct Betclic/Betano acquisition remains experimental and unsupported where no
exact profile is installed. No additional live probing is part of the v1 path.
Current prices can instead use strict canonical CSV/JSON or the shared manual
validator. Final placement remains manual.

See [the coherent football product](docs/football-product.md),
[the model tournament](docs/model-tournament.md), and
[the market capability matrix](docs/market-capabilities.md).

## Runtime constraints

- Runtime operation does **not** depend on paid APIs.
- Runtime operation does **not** depend on external AI, LLM, or cloud inference services.
- Development tooling may use AI assistance, but the final application will not require it.

## Current status

This repository is the **local v1.0.0 release**. It is a single-operator,
localhost-only operational workspace with narrowly allowlisted local writes for
system preparation, upcoming matches, current odds, and product refresh. Final
bookmaker placement remains manual. It does not claim profitability.

## Normal operator path

Regular operation does not require terminal commands:

1. Open the repository in VS Code.
2. Select **Sports Analytics Local MVP** in Run and Debug.
3. Press **F5**.
4. Open the printed `http://127.0.0.1:8501` URL.
5. Confirm and select **Prepare system**.
6. Add upcoming matches on **Matches**.
7. Add real offered odds on **Odds**.
8. Review automatically generated singles and accumulators on **Bets**.
9. Place an eligible proposal manually at the bookmaker.

F5 uses the selected VS Code Python interpreter, performs safe idempotent
initialization, starts the durable worker and Streamlit UI, prints the loopback
URL, and never opens a browser automatically. The CLI remains available for
diagnostics, backup, restore, and advanced operations.

Implemented now:

- packaging, typed configuration, local runtime bootstrap, and logging;
- SQLite operational persistence with forward-only migrations `0001`–`0005`;
- durable local job worker infrastructure and a local supervisor that can also
  start the bookmaker scheduler when enabled;
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
- an operator-first Streamlit interface with Dashboard, Bets, Matches, Odds,
  History, and System navigation; strict human-friendly match and offered-odds
  input; automatic product refresh; candidate and accumulator display; and
  advanced immutable audit access under System;
- canonical result snapshots, deterministic analytical settlement,
  persisted-evidence monitoring, and explicit champion–challenger governance;
- Betano Portugal / Betclic Portugal bookmaker acquisition foundation (ordinary
  headless Chromium by default, fixed public routes, raw captures, v1 canonical
  and v2 provider-native snapshots, selection policy, local scheduler,
  migration `0005`);
- documentation, linting, typing, and tests.

**Not implemented**: login or bet placement; CAPTCHA / anti-bot bypass; guaranteed
indefinite live browser acquisition; additional sports beyond football /
basketball / tennis pre-match scope; markets beyond the initial canonical
mapping set; Kelly staking; bet recommendations; real bookmaker settlement;
bankroll management; cross-source fuzzy resolution.

## Supported Python version

Python **3.12** or newer is required.

## Prerequisites

- Python 3.12+
- `pip` and the standard library `venv` module
- Git

## CLI diagnostics and compatibility

Normal launch now initializes automatically. The equivalent CLI path is:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\sports-analytics-v1.exe
```

Explicit initialization and doctor remain available:

```powershell
.venv\Scripts\python.exe launch_v1.py --initialize
.venv\Scripts\python.exe launch_v1.py --doctor
```

The website is printed once and served only at
`http://127.0.0.1:8501`. Press `Ctrl+C` in the operator terminal to stop the
worker and website cleanly. `--worker-once` processes at most one available job
and intentionally skips the temporary UI.

Initialization is idempotent and applies packaged migrations `0001`–`0005`.
Doctor is strictly read-only: `ready` and `degraded` return success,
`not-initialized` returns exit code 3, and invalid software/configuration returns
exit code 2. An absent champion, absent real offered odds, or an expected
economic-evidence hold may degrade analytical availability without making the
installation corrupt.

Back up and restore with:

```powershell
.venv\Scripts\sports-analytics-v1.exe --backup C:\backups\sports-analytics-v1-2026-07-30
.venv\Scripts\sports-analytics-v1.exe --restore C:\backups\sports-analytics-v1-2026-07-30
```

Restore is fail-closed and only accepts absent or empty configured persistent
destinations. It never force-overwrites operational state. See the
[v1 operator runbook](docs/v1-operator-runbook.md) and
[v1 release notes](docs/release-v1.md).

Direct bookmaker acquisition is not required. Current real offered odds can be
uploaded as canonical CSV/JSON or entered in the editable Odds table. The
application validates them, calculates probability, fair odds, edge, and EV,
and generates supported singles and accumulators automatically. Economic holds
remain visible and truthful; they are not ready for placement. There is no
profitability guarantee.

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

### Playwright Chromium (bookmaker acquisition)

Bookmaker acquisition uses ordinary Playwright Chromium automation on localhost
only. Headless is the default; visible and best-effort minimized visible modes
remain available. All modes use the same allowlists, bounds, block detection,
and sanitization. After installing the package, install the browser once:

```bash
python -m playwright install chromium
```

Bookmakers remain **disabled by default** (`bookmakers.enabled = false`). Enable
only for local operator use after reviewing current provider terms. There is no
login, no bet placement, and no CAPTCHA or anti-bot bypass. Blocked providers are
classified and cooled down; the last valid immutable snapshot may be preserved as
stale/unavailable, never labelled current. Live acquisition is best-effort and
is **not** guaranteed to work indefinitely as sites change.

The production transport is browser-observed: Chromium navigates a fixed public
provider route, the provider page naturally initiates its own network activity,
and a `page.on("response")` observer may retain only bounded, structurally
approved response bodies from that same page and acquisition cycle. Provider
adapters never replay discovered endpoints through `requests`, `httpx`,
Playwright request contexts, copied cURL, cookies, headers, tokens, or forged
request bodies. Chromium supplies its ordinary browser identity.

## Bookmaker acquisition (PR #13 implementation pass)

Architecture is localhost-only: SQLite jobs + Parquet snapshots on the local
filesystem. Betclic (`betclic-pt`) is the priority provider integration, with
football first and basketball/tennis inspected sequentially. Passive page-scoped
Chromium streaming retained complete gRPC-Web data frames for football,
basketball, and tennis while their HTTP responses remained open. Passive
inspection of already-loaded approved client scripts established a protobuf-ts
runtime and distinct `GetPopularV2`/`GetLiveCount` response objects, but did not
establish the event response field table. A trailer is not required to parse an
already complete data message; it remains separate evidence for logical RPC
completion. No event semantics or exhaustive-inventory mechanism was
established, so no Betclic Stage-A or detail profile is registered. Betano
(`betano-pt`) remains experimental and lower priority.
Existing non-bookmaker sources retain their historical and analytical roles;
they are not current-odds sources. Initial bookmaker scope is football,
basketball, and tennis pre-match only.

Every job carries an exact half-open UTC event horizon. The default is the next
48 hours, the configurable hard maximum is 168 hours, and the default per-sport
event cap is 100. Refresh cadence controls how often acquisition runs; the
event horizon independently controls which scheduled events are admitted.

`bookmaker-native-v2` snapshots preserve typed provider-native events, markets,
selections (including explicitly unpriced or suspended observations), nullable
prices with a typed price state, lines, statuses, exact capture references, and
explicit completeness counts before canonical projection. Unknown markets and
valid unpriced or suspended selections are retained in native inventory and are
never silently comparable. Canonical outcomes require an exact reviewed parser
mapping; provider-owned selection IDs remain source identities and are never
outcome fallbacks. Existing
`bookmaker-canonical-v1` snapshots remain loadable without changing their
contract.

Provider/sport verification is exact, with no provider-only, sport-only, or
default profile fallback. The existing Betano football
`topEventsV2` profile is verified only for its reviewed landing inventory and is
explicitly incapable of proving exhaustive event-detail coverage. Betano
basketball/tennis and all Betclic sport profiles remain disabled until real
event-detail structures and stable identities are evidenced. A landing or
popular-event response is always `unknown-completeness`, never exhaustive.
The provider-neutral Stage-B planner/executor extension is wired into the
production adapter call chain but disabled for every current provider/sport
profile. Its end-to-end traversal test uses only a synthetic capability and
executor; it does not establish operational event-detail extraction.
This pass adds a bounded passive Chromium streaming path but remains
foundation-only. Complete transport frames are individually parseable even
while the HTTP/RPC remains open, but do not by themselves establish a semantic
event profile, logical RPC completion, or exhaustive inventory; synthetic
fixtures and static mappings never raise an operational classification.

The default body limit is 2 MiB per response and 16 MiB per cycle. Oversize or
exhausted-budget responses are explicit partial evidence. Event-detail traversal
is bounded by a concurrency-one, fixed one-second minimum interval, 30-second
navigation-timeout, zero-retry contract if a reviewed provider profile is later
enabled; no current profile enables that path. An
explicit access denial, CAPTCHA, regional refusal, or anti-automation block
stops the cycle immediately.

For Chromium only, a page-scoped CDP session may passively observe an exact
approved streaming response without interception, mutation, replay, copied
request material, or WebSocket frames. Support is detected at runtime;
unsupported versions retain the finite response path and report
`streaming-body-observation-unsupported`. Responses, queued observations,
chunks, bytes, frames, frame payloads, incomplete trailing bytes, first-data
time, idle time, and total stream lifetime are all bounded independently.
The same page-scoped session may enable the Debugger domain before navigation
to inspect only already-loaded scripts from exact approved hosts. Source text is
ephemeral: diagnostics retain only source/path hashes, bounded presence
evidence, safe candidate type names, field-number/type sequences, and
method-to-response associations. No asset is fetched separately and no script
source is persisted.

Same-bookmaker multiple invariant: a multiple is valid only when every leg uses
quotes from exactly one bookmaker. Betano-only and Betclic-only totals are
compared as complete slips; equal totals select Betano. Mixed-provider singles
use `CrossBookmakerSinglesComparison` and are never labelled or stored as a
multiple.

Useful local commands (with bookmakers enabled in settings):

```bash
python scraper.py --list-bookmaker-capabilities
python -m sports_analytics.bookmakers --help
python run_local.py --config config/settings.toml
```

The probe command remains a bounded diagnostic implementation, but it is not a
v1 product prerequisite and should not be run as part of the football modelling
workflow. The supported v1 current-price path is strict offline operator input.

`run_local.py` starts the worker and, when `bookmakers.enabled` is true and not
`--worker-once`, also starts the bookmaker scheduler. Operator status remains
available through database/status CLIs and provider-status records; review
provider terms before enabling live acquisition.

Localhost operation, internal analysis, and technical profile verification are
not claims of legal authorization. Operators must separately review current
provider terms and applicable law before enabling acquisition; captured data is
not intended for republication.

Offline synthetic fixtures under `tests/fixtures/betano/` and
`tests/fixtures/betclic/` drive parser and domain tests without live pages.

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

`run_local.py` supervises the worker child process and, when
`bookmakers.enabled` is true (and not `--worker-once`), also supervises the
bookmaker scheduler. Streamlit is not supervised here. The frozen default
handler registry includes `system.noop`, `ingest.football-data-csv`,
`ingest.bookmaker-current-odds`, settlement, and monitoring handlers.

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
sports) without touching the database or the network. Registered adapters include
Football-Data.co.uk plus Betano/Betclic bookmaker current-odds descriptors.

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
- **Ingestion coordinator** (`scraper.py`) for permitted public sources
  (Football-Data.co.uk plus bookmaker enqueue/status CLI surfaces).
- **Engine** (`engine.py`) for deterministic features, local models, prediction
  contracts, value evaluation, and historical backtesting (implemented).
- **Worker** (`worker.py`) for durable background jobs, leases, and retries
  (implemented), including `ingest.bookmaker-current-odds`.
- **SQLite** for operational state, jobs, snapshot metadata, bookmaker run/status,
  and audit records.
- **Parquet** for historical and analytical datasets under versioned immutable
  snapshots, including `current-bookmaker-odds`.

See [docs/architecture.md](docs/architecture.md) for principles and boundaries.
See
[docs/settlement-monitoring-governance.md](docs/settlement-monitoring-governance.md)
for canonical result evidence, analytical settlement, monitoring, and explicit
champion–challenger operations.
See [docs/closed-loop-learning.md](docs/closed-loop-learning.md) for verified
result projection, training eligibility, periodic challenger evaluation, and
manual promotion/rollback. See
[docs/player-evidence.md](docs/player-evidence.md) for the current
display-only player evidence boundary.

The granular PR #13 operator surface is available with:

```powershell
python -m sports_analytics.services.lifecycle_cli --help
```

## Current limitations

- Historical Football-Data.co.uk ingestion remains the only non-browser CSV
  adapter. Bookmaker adapters for Betano Portugal and Betclic Portugal exist for
  pre-match football/basketball/tennis current odds, but live browser acquisition
  is best-effort and not guaranteed indefinitely.
- Bookmaker acquisition never logs in, places bets, or bypasses CAPTCHA/anti-bot
  controls. Blocked providers are classified and cooled down; stale cache is never
  labelled current.
- Same-bookmaker multiples only; mixed-provider collections are singles
  comparisons, not multiples.
- Only two Football-Data competitions are supported for historical CSV
  ingestion; historical 1X2 is the production Football-Data market.
- Cross-source resolution is limited to exact canonical identity. There is no
  fuzzy or machine-learning matching and no silent alias merge; unresolved source
  events stay in `source_events` and are excluded from downstream-safe datasets.
- No current-price production opportunity feed, bookmaker settlement, staking,
  correlation model, or bankroll management. Deterministic flat-unit analytical
  settlement of verified persisted positions is implemented.
- Analytical datasets remain immutable artifacts. SQLite additionally stores
  minimal operational result, settlement, monitoring, model-role, and bookmaker
  acquisition indexes.
- Ordinary headless/visible Playwright automation is used only for allowlisted
  bookmaker public routes when enabled; there is no arbitrary URL/HTML scraping
  surface and no stealth or access-control evasion.
- `run_local.py` supervises the worker and optional bookmaker scheduler; it does
  not start Streamlit, which is launched explicitly.
- Durable handlers include football ingestion, bookmaker current-odds ingestion,
  deterministic analytical settlement, persisted-evidence monitoring, verified
  result registration, retraining trigger evaluation, and challenger cycles.
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
