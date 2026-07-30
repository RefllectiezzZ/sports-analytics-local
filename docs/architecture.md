# Architecture

This document describes the architecture for `sports-analytics-local`.

The repository currently provides packaging, typed configuration, local runtime
bootstrap, SQLite operational persistence, migrations through `0005`, durable
local job worker infrastructure, a local supervisor that can start the bookmaker
scheduler, Football-Data.co.uk ingestion that publishes immutable Parquet
snapshots, Betano/Betclic bookmaker acquisition for current pre-match odds,
sport-agnostic canonical data contracts, a leakage-safe football full-match 1X2
modelling pipeline, documentation, generic prediction/value/opportunity/
combination/backtesting contracts, a football closing-line historical singles
benchmark, and quality tooling, plus a read-only Streamlit interface over
verified typed analysis and backtest artifacts. Login, bet placement, CAPTCHA
bypass, and guaranteed indefinite live browser acquisition are **not**
implemented.

## Entry points

| Component | Role |
| --- | --- |
| `app.py` | Streamlit entry point for local interactive analytics and review. |
| `scraper.py` | Coordinates data ingestion from permitted public sources. |
| `engine.py` | Coordinates feature generation, prediction, and combination generation. |
| `worker.py` | Executes durable background jobs outside the Streamlit process. |
| `run_local.py` | Supervises the local worker and optional bookmaker scheduler. |

Each root script supports shared configuration and database modes:
`--validate-config`, `--database-status`, and `--migrate-database`. The worker
entry point also exposes queue status, expired-lease recovery, and worker-run
modes.

`scraper.py` is implemented: it lists source descriptors (`--list-sources`) and
competitions (`--list-competitions`), enqueues football ingestion jobs
(`--enqueue-football-data`), lists snapshot metadata (`--list-snapshots`), and
verifies READY snapshots read-only (`--verify-snapshot`). `engine.py` builds
football 1X2 feature artifacts, trains/calibrates/evaluates the logistic baseline,
verifies model artifacts, and runs explicit-parameter inference. `app.py` renders
the read-only artifact interface when launched through Streamlit and retains the
shared configuration/database CLI modes for regular Python invocation.

## Domain boundaries

### Coherent football probability and product boundary

The primary football path is persisted-data-first:

`historical results -> rolling tournament -> reviewed joint score distribution
-> derived market probabilities/fair odds -> optional strict offered quotes ->
opportunities/proposals -> read-only UI`.

`models.football_scores` owns football scoring assumptions. Shared market and
artifact contracts do not contain football score rules. Every supported
full-time goal market is a fixed predicate over one bounded score matrix.
`bookmakers.operator_quotes` is the offline current offered-price boundary and
cannot carry URLs, request material, scripts, profiles, or credentials.
`services.football_product` orchestrates immutable artifacts; the Streamlit
process reads only the final verified read model.

Basketball and tennis have explicit `model-unavailable` capability rows and do
not reuse the football generator. First-half, corners, shots, player, and live
families remain evidence-gated as documented in `market-capabilities.md`.

The code separates four concerns that earlier revisions conflated.

| Package | Responsibility |
| --- | --- |
| `sports_analytics.sports` | Sport-agnostic canonical identity, reconciliation, and Arrow schemas |
| `sports_analytics.markets` | Sport-agnostic betting market definitions, selections, and quotes |
| `sports_analytics.snapshots` | Domain-neutral snapshot specification, writing, manifests, publication, verification |
| `sports_analytics.sources` | Source catalog with roles and capabilities, HTTP retrieval, raw storage, parsing |

`sports_analytics.sports.football` holds the football projection of the shared
contracts, and `sports_analytics.ingestion` wires everything into the worker and
the scraper CLI.

### Identity boundary

Canonical participant and event identities are deterministic UUIDv5 values
derived only from source-independent facts. Participant identity is scoped by
sport, participant type, `participant_identity_scope`, and normalized canonical
name, never by `source_name`, `competition_id`, season, or membership. Football
clubs currently use provisional scopes such as `club:england` and
`club:portugal`. Event identity is scoped by sport, competition, season,
canonical participants, and `event_occurrence_key`; `event_date` and kickoff are
mutable scheduling metadata.

Source-scoped identities (`source_participant_id`, `source_event_id`,
`source_event_key`) always include `source_name` and exist for provenance and
adapter tracing. Canonical `events` rows are unique by `canonical_event_id` and do
not carry per-source duplication. The `source_events` dataset retains every
source event candidate, including unresolved rows, with row-level provenance.
Source-scoped entities are never described as canonical.

Participant and event reconciliation are conservative and versioned
(`participant-reconciliation-v1`, `event-reconciliation-v1`): only `exact`
matches are produced automatically, and unresolved candidates are recorded with a
reason instead of being merged. There is no fuzzy matching or silent alias merge:
different normalized names stay distinct unless an explicit approved alias mapping
unifies them.

When multiple downstream-safe source events resolve to one canonical event,
immutable identity dimensions must agree. Conflicting finished outcomes raise
`SourceIntegrityError`. For non-finished events, mutable scheduling and status
metadata is selected by most recent observation, then source authority, then
lexicographic source name. Once an agreeing finished outcome exists, canonical
metadata is selected only from those finished source events and newer
non-finished observations cannot downgrade it. Every original source event
remains auditable in `source_events`.

### Market boundary

Betting markets use one generic contract (`MarketDefinition`,
`MarketSelection`, `OddsQuote`) with validated extensible dimensions rather than a
per-market dataset. Only football full-match 1X2 is emitted by a production
adapter; a synthetic totals fixture in the test suite proves the contract
generalizes to line markets.

### Snapshot boundary

The snapshot package never imports a sport, market, or ingestion package. It
receives every domain fact through a validated `SnapshotSpec` with generic
partition keys, a `SnapshotDatasetSuite`, and `domain_metadata`.
`tests/unit/snapshots/test_import_boundary.py` enforces that boundary. Suite
resolution for verification lives in
`sports_analytics.ingestion.snapshot_specs.resolve_snapshot_suite`.

## Ingestion boundary

Football ingestion is split into explicit layers:

1. **Source adapter** (`sports_analytics.sources`) — static catalog with roles and
   capabilities, allowlisted HTTPS download, content-addressed raw storage, strict
   CSV parsing.
2. **Canonical normalization** (`sports_analytics.sports`,
   `sports_analytics.markets`, `sports_analytics.sports.football`) — versioned
   `football-canonical-v2` records, reconciliation decisions, generic market
   quotes, and explicit PyArrow schemas.
3. **Parquet snapshots** (`sports_analytics.snapshots`) — deterministic multi-file
   snapshot directories, `snapshot-manifest-v2` manifests, publication, and
   verification.
4. **Worker integration** (`sports_analytics.ingestion`) — job handler
   `ingest.football-data-csv`, enqueue service, snapshot spec construction, and
   the scraper CLI.

Long-running HTTP, CSV, and Parquet work never holds an open SQLite transaction.
Handlers never receive a raw SQLite connection. `RuntimeContext` never contains an
open connection.

See [sources.md](sources.md), [data-contracts.md](data-contracts.md), and
[snapshots.md](snapshots.md).

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
  is idempotent. It never creates the SQLite database file itself.
- Relative paths resolve against the supplied base directory, not against an
  imported module location. Absolute paths remain absolute.

## Runtime context

`bootstrap_runtime` loads settings, resolves paths, creates runtime directories,
seeds deterministic generators, configures logging, ensures the operational
SQLite database is migrated, and returns an immutable `RuntimeContext` containing
the component name, settings, paths, UTC startup timestamp, component logger,
`database_path`, and `schema_version`.

`RuntimeContext` does **not** store an open `sqlite3.Connection`.

`--validate-config` uses `validate_configuration`, which loads and resolves
settings without creating directories, writing log files, configuring persistent
handlers, seeding global random state, or touching SQLite.

## SQLite operational persistence boundary

- Standard-library `sqlite3` only. No SQLAlchemy, Alembic, or other ORM /
  migration dependencies.
- Explicit connection ownership via `connect_database`.
- Explicit transaction ownership via `transaction`.
- Repositories receive an explicit connection and never commit on their own.
- Worker queue operations use `WorkerService`, which owns short-lived SQLite
  connections and transaction boundaries for claim, heartbeat, recovery, and
  finalization calls.
- Forward-only packaged SQL migrations with immutable checksums.
- Automatic idempotent migration during normal bootstrap.
- Read-only inspection for `--database-status`.

See [database.md](database.md) for connection/transaction rules, migration
policy, CLI behaviour, and tables.

### Operational tables

- `application_metadata` — durable key/value application metadata
- `jobs` — durable background-work records and lease ownership
- `job_events` — append-only job lifecycle events
- `worker_instances` — durable worker process metadata and heartbeats
- `snapshots` — metadata for immutable Parquet snapshots
- `audit_events` — append-only application audit trail

There are no sports-domain SQLite tables. Analytical sports data lives in
immutable Parquet snapshots; SQLite stores only snapshot metadata. Sports-domain
SQL schemas (predictions, bets, bankroll, features, model training) remain future
work and may never be needed for analytical datasets.

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

## Storage principles

- **SQLite** stores operational state, metadata, jobs, snapshot metadata, and
  audit records.
- **Parquet** stores historical and analytical datasets.
- **Snapshots** are immutable once marked ready, at both the metadata and
  filesystem layers.
- Runtime-generated files live under `storage/`; only `.gitkeep` markers are
  tracked in Git.

## Processing principles

- Processing is **deterministic**, including seeded modelling where applicable.
- Contracts, schemas, policies, and producers are **versioned** and recorded in
  every manifest.
- Sports models and market models will eventually be **separated**; the canonical
  contracts already separate sport facts from market facts.
- Failures are **explicit and auditable**: no silent data loss, no silent merges
  of ambiguous identities, and no invented values for unknown source data.

## Durable worker boundary

The local worker is a separate process boundary from future Streamlit UI code. It
is intentionally sequential: one claimed job executes at a time per worker
process. The worker uses a static in-process `HandlerRegistry`, freezes it before
execution, and exposes registered job types as worker capabilities. Job payloads
do not select import paths or executable code.

The frozen default registry contains `system.noop` (infrastructure only) and
`ingest.football-data-csv` (football ingestion).

Queue coordination is SQLite-backed:

- `WorkerService` owns short-lived connections and explicit transactions;
- `claim_next_job` atomically moves one available job from `pending` to
  `running`;
- claim order is `priority`, `available_at`, `created_at`, then `id` ascending;
- `lease_owner` is the worker UUID, not a PID or hostname;
- a daemon heartbeat renews the running job lease while the handler runs;
- expired leases are recovered into delayed pending retries or terminal failure;
- retry backoff is deterministic: `min(max, base * 2**(attempts-1))`;
- execution is at-least-once, not exactly-once.

Handlers run outside SQLite transactions. They must be idempotent before they
perform side effects, because a crash after external work but before queue
finalization can lead to another attempt after lease expiry. Snapshot READY reuse
is what makes the football ingestion handler idempotent at the snapshot layer.

`run_local.py` currently supervises only `worker.py`. It validates configuration,
ensures the database is migrated, starts the worker with `sys.executable` and
`shell=False`, propagates the worker exit code, and uses
`worker.shutdown_grace_seconds` before force-killing a stubborn child. A later
phase will add the Streamlit child process.

See [worker.md](worker.md) for the full worker and queue lifecycle.

## External dependency boundaries

- External AI, LLM, or cloud inference services will **not** be runtime
  dependencies.
- Paid APIs will **not** be required for runtime operation.
- Ingestion uses **permitted public sources only**.

## Package layout

Application code lives under `src/sports_analytics/` with focused subpackages:

- `core` — configuration, paths, logging, runtime bootstrap, shared CLI
- `data` — SQLite persistence, migrations, repositories, dataset I/O
- `jobs` — handler registry, worker service, worker runner
- `local` — local process supervisor
- `sources` — source catalog, HTTP retrieval, raw storage, source parsing
- `sports` — sport-agnostic canonical contracts, identifiers, reconciliation,
  schemas, and the `football` projection
- `markets` — generic market contracts, identifiers, and schemas
- `snapshots` — sport-agnostic snapshot specs, writing, manifests, publication,
  verification
- `ingestion` — ingestion service, job handler, snapshot specs, scraper CLI
- `scrapers` — reserved for future browser-free scraping helpers (empty)
- `features` — football pre-match feature contracts and leakage-safe builders
- `models` — logistic baseline, temperature calibration, pickle-free artifacts
- `predictions` — complete immutable probability records and verified lineage
- `value` / `opportunities` — complete-market edge/EV, filters, audit, ranking
- `combinations` — dependency classification, validation, bounded construction
- `backtesting` — rolling-origin contracts, pure settlement, required metrics
- `services` — training/backtesting orchestration and engine CLI
- `evaluation` — temporal folds and probability metrics

## Current implementation status

Implemented:

- typed configuration loading and validation;
- path resolution and safe runtime directory creation;
- local logging configuration;
- deterministic seeding;
- shared runtime bootstrap and CLI options;
- SQLite connection/transaction foundation;
- forward-only migrations through schema version 4;
- typed repositories for metadata, jobs, snapshots, and audit events;
- database status / migrate CLI modes;
- durable worker claiming, lease heartbeats, recovery, and static handler
  registry;
- football ingestion adapter and snapshot publication;
- football 1X2 feature artifacts, rolling-origin training, calibration, and
  explicit model artifacts via `engine.py`;
- worker-only `run_local.py` supervisor;
- one Football-Data.co.uk ingestion adapter with allowlisted HTTPS retrieval,
  content-addressed raw storage, and strict CSV parsing;
- two football competitions (`eng-premier-league`, `prt-primeira-liga`);
- participant-identity-scope canonical participants, occurrence-key canonical
  events, and source participant/event provenance datasets;
- conservative versioned participant and event reconciliation;
- the generic canonical market quote contract, with historical 1X2 mapped into
  it;
- immutable generic Parquet snapshots with `snapshot-manifest-v2`;
- the `ingest.football-data-csv` handler in the frozen default registry;
- snapshot listing and verification through `scraper.py`;
- generic prediction, value, opportunity, combination, and backtest contracts;
- football 1X2 Football-Data closing-market singles benchmark and strict
  content-addressed analytical artifacts;
- read-only Streamlit artifact catalogue, opportunity, accumulator-preview,
  backtest, and audit pages wired to strict typed artifact verification.
- canonical result contracts and immutable verified result snapshots;
- deterministic flat-unit analytical settlement for persisted football 1X2
  opportunities and exact persisted combinations;
- persisted-evidence operational/model monitoring with explicit unknown states;
- verified champion–challenger registry, immutable evaluation decisions, atomic
  promotion application, and audited rollback.

Explicitly **not** implemented yet:

- login, bet placement, CAPTCHA/anti-bot bypass, or guaranteed indefinite live
  browser acquisition against changing provider sites;
- additional sports beyond the initial football/basketball/tennis pre-match
  bookmaker scope;
- markets beyond the initial exact canonical bookmaker mappings plus historical
  Football-Data 1X2;
- production current-price opportunity discovery UI accumulators;
- bookmaker settlement, staking, and bankroll management;
- cross-source fuzzy resolution;
- Streamlit child process spawning in `run_local.py`.

## Bookmaker acquisition

The bookmaker acquisition foundation provides:

- Fixed public HTTPS routes for `betano-pt` and `betclic-pt` only.
- Ordinary headless Chromium by default, with visible and best-effort minimized
  visible presentation modes under identical safety policy.
- Content-addressed minimized raw captures and immutable
  `current-bookmaker-odds` snapshots (`bookmaker-canonical-v1`).
- Betclic is the priority provider integration, but is not operational.
  Passive Chromium streaming retained stable complete data frames for football,
  basketball, and tennis. The responses remained open. Approved-client
  inspection established a protobuf-ts runtime and exact reviewed RPC
  associations, but not the event response field table. No body-derived
  semantic profile was installed. Betano remains experimental and lower
  priority.
- Same-bookmaker multiple invariant; mixed-provider singles comparison is a
  separate type and never a multiple.
- Explicit fallback: Betclic first; stale last-valid snapshot may be preserved
  but never labelled current; no third-party fallback enabled by default.
- Durable job `ingest.bookmaker-current-odds`, local scheduler, and migration
  `0005` operational tables.
- An immutable exact UTC acquisition window (`bookmaker-acquisition-window-v1`)
  separating refresh cadence from the upcoming-event horizon.
- A provider-neutral, deterministic Stage-B event-navigation extension wired
  into the production adapter call chain. Provider-derived targets require
  exact HTTPS host approval and a reviewed provider-specific event-path grammar;
  no arbitrary URL is accepted from a job. All current provider/sport
  capabilities are explicitly disabled, and only an offline synthetic
  capability/executor exercises the extension end to end.
- A backward-compatible `bookmaker-native-v2` suite that preserves native
  events, markets, priced and explicitly unpriced selections, nullable exact
  decimals, per-row evidence links, and completeness counts before the reviewed
  canonical subset.

The transport call chain is:

```text
Playwright Chromium
-> fixed provider-owned public route
-> provider page naturally generates network activity
-> page.on("response") observes the response in the active cycle
-> allowlisted host plus reviewed structural response gate
-> finite path: requestfinished then bounded body capture/checksum
-> Chromium streaming path: page-scoped passive CDP correlation, bounded queue,
   incremental framing, and complete-frame capture while HTTP may remain open
-> passive client path: already-loaded approved-host scripts only, ephemeral
   source inspection, safe descriptor hashes/type associations only
-> optional provider-owned Stage-B candidates and reviewed navigation plan
   (disabled for every current provider/sport profile)
-> provider-native parser
-> immutable snapshot publication
-> strict reload
```

There is no independent bookmaker HTTP client. The runtime does not reconstruct
or replay observed REST, GraphQL, or gRPC requests and does not copy browser
headers, cookies, tokens, or request bodies. Safe transport metadata distinguishes
document, fetch, XHR, gRPC-Web, EventSource, script/configuration, and other
approved public responses. WebSocket connection metadata is admitted only for
`wss://` on the exact approved public hostname and retains only the hostname and
path hash plus acquisition identity and time. Raw WebSocket frames, HTML,
scripts, complete URLs, queries, fragments, and headers are not persisted.
No script asset is fetched separately. Script text, source-map URLs, and full
script URLs remain ephemeral and are never diagnostic fields.

Each retained response is linked to provider, sport, acquisition cycle, and page
route. It records request method, safe transport type, approved hostname,
approved route classification or path hash, status/content type, declared and
captured lengths, redirect class, capture/truncation state, observation time,
and a content checksum. Configuration JSON or URL keywords alone do not satisfy
an extraction profile.

Completeness is event-scoped. Only provider-reference equality or a reviewed
structurally complete event-detail payload can produce a complete state.
Landing/popular-event evidence remains `unknown-completeness`; deadlines,
truncation, navigation failure, and parser rejection produce explicit partial
states. Unknown markets may publish in native inventory but never become
canonical or comparable without a reviewed mapping. Provider selection IDs
remain source identities rather than canonical-outcome fallbacks. Multi-chunk
completeness requires one unique chunk for each expected sequence; duplicate
sequence claims fail closed.

Verification is provider/sport specific. At this implementation point only the
Betano football landing-inventory profile is registered, and its completeness
capability is explicitly non-exhaustive. Betano basketball/tennis and Betclic
football/basketball/tennis remain unverified rather than borrowing the football
profile or guessed protobuf fields. Registry lookup requires both provider and
sport; provider-only, sport-only, and default fallback do not exist.

The streaming observer enables the Network domain before navigation and detects
experimental resource-stream support only when an exact approved candidate is
seen. It never enables Fetch interception, pauses or modifies a request,
replays a URL, copies request material, or observes WebSocket frames. CDP
request IDs and complete URLs remain cycle-local and are excluded from
diagnostics. Callback handlers only enqueue bounded observations; one consumer
performs correlation, decoding, raw evidence storage, and cleanup.

The same page-scoped session may enable Debugger before navigation. Script
inspection is limited by scripts seen/inspected, declared and actual source
characters, total source characters, matches, field entries, queue size, and
wall time. Exact host approval happens before `getScriptSource`; cross-provider,
extension, data, and blob scripts are rejected. Only runtime-family presence,
safe candidate type names, field-number/type sequences, reviewed
method-to-response associations, and hashes survive the inspection.

HTTP completion, complete data-frame observation, final trailer observation,
logical RPC completion, parsing sufficiency, event completeness, and market
completeness are independent states. A complete frame may be inspected while
HTTP remains open and does not need a trailer merely to be parsed. Only a valid
final trailer can establish logical RPC completion for this reviewed transport.
Neither a 2xx response nor a complete data frame establishes exhaustive
inventory.

Operators must review current provider terms before enabling
`bookmakers.enabled`.
Technical evidence verification and legal authorization are separate. Localhost
or private analytical use does not itself grant access permission.
