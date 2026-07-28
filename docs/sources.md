# External sources

Source adapters retrieve permitted public sports data into content-addressed raw
storage and feed canonical snapshot ingestion.

## Source roles and capabilities

Different sources serve different purposes, so every registered adapter declares
an explicit role and an explicit capability set. Callers ask what a source can
actually do instead of assuming one behaviour for every source.

Roles (`SourceRole`):

| Role | Meaning |
| --- | --- |
| `historical-data` | Publishes historical results, statistics, or prices |
| `bookmaker` | Publishes current bookmaker markets and prices |
| `fixture-calendar` | Publishes upcoming fixtures |
| `results-feed` | Publishes results for settlement purposes |

Capabilities (`SourceCapability`):

| Capability | Meaning |
| --- | --- |
| `historical-results` | Past event results |
| `historical-statistics` | Past event statistics |
| `historical-odds` | Past prices |
| `current-fixtures` | Upcoming fixture inventory |
| `current-odds` | Currently offered prices |
| `settlement-results` | Results usable for settling placed bets |

Unknown role or capability strings raise a typed `PermanentSourceError` rather
than silently resolving to `False`. `require_capability` lets a caller fail fast
when an adapter cannot do what is being asked of it.

### Registered adapters

Only implemented adapters appear in the catalog.

| source_id | display_name | role | adapter_version | capabilities | sports |
| --- | --- | --- | --- | --- | --- |
| `football-data-co-uk` | Football-Data.co.uk | `historical-data` | `football-data-co-uk-adapter-v1` | `historical-odds`, `historical-results`, `historical-statistics` | `football` |
| `betano-pt` | Betano Portugal | `bookmaker` | `betano-pt-adapter-v1` | `current-fixtures`, `current-odds` | `basketball`, `football`, `tennis` |
| `betclic-pt` | Betclic Portugal | `bookmaker` | `betclic-pt-adapter-v1` | `current-fixtures`, `current-odds` | `basketball`, `football`, `tennis` |

Betano/Betclic adapters acquire only allowlisted public HTTPS routes through a
visible Playwright Chromium session. They do not accept arbitrary URLs, do not
log in, do not place bets, and do not bypass CAPTCHA or anti-bot controls.
Blocked pages stop the acquisition attempt and are classified explicitly.
Offline synthetic fixtures under `tests/fixtures/betano/` and
`tests/fixtures/betclic/` drive parser tests without live pages.

Live browser acquisition is best-effort and not guaranteed to keep working as
provider sites change. Operators remain responsible for reviewing current
provider terms before enabling acquisition.

`scraper.py --list-sources` prints one tab-separated descriptor line per adapter
with no database or network access:

```text
source_id<TAB>display_name<TAB>role<TAB>adapter_version<TAB>capabilities<TAB>supported_sports
```

Capabilities and supported sports are comma-separated in deterministic sorted
order.

## Football-Data.co.uk

Source identifier: `football-data-co-uk`

This release implements exactly one ingestion adapter. Production ingestion
constructs CSV URLs from a static project-owned catalog. It does **not**:

- scrape HTML pages at runtime;
- launch a browser;
- accept arbitrary URLs, hosts, division codes, or local paths from job payloads;
- require an API key.

Users are responsible for reviewing and respecting the source's current terms and
permitted use. The project does not bypass authentication, paywalls, access
controls, rate limits, robots restrictions, or anti-bot measures.

### Fixed URL construction

```text
https://www.football-data.co.uk/mmz4281/{source_season_code}/{division_code}.csv
```

Example: Premier League 2023-2024 becomes
`https://www.football-data.co.uk/mmz4281/2324/E0.csv`

### Supported competitions

| competition_id | display_name | country | division | timezone |
| --- | --- | --- | --- | --- |
| `eng-premier-league` | Premier League | ENG | E0 | Europe/London |
| `prt-primeira-liga` | Primeira Liga | PRT | P1 | Europe/Lisbon |

Both use cross-year seasons (`YYYY-YYYY`).

### Season syntax

- exact `YYYY-YYYY`
- end year = start year + 1
- no whitespace, signs, or two-digit canonical input
- conservative supported start-year range documented in code

### HTTP safety

- HTTPS only
- exact host allowlist: `www.football-data.co.uk`
- normal TLS verification
- bounded timeout, retries, pacing, and download size
- automatic urllib redirect following is disabled
- every redirect is validated before any connection attempt to the destination
- redirects must remain HTTPS on the exact host `www.football-data.co.uk` with the
  permitted port and no embedded credentials
- cross-host, HTTP downgrade, file, localhost, loopback, private-network, missing
  Location, and redirect-loop destinations are rejected without contacting them
- response body is streamed directly into the content-addressed raw store while
  hashing and enforcing `maximum_download_bytes` (default 25 MiB, max 100 MiB)
- apparent HTML bodies rejected
- cookies are never stored or logged
- proxy credentials are never logged

### Retry and pacing

Configured under `[scraping]`:

- `request_timeout_seconds`
- `maximum_retries`
- `retry_backoff_base_seconds`
- `retry_backoff_max_seconds`
- `minimum_request_interval_seconds`
- `maximum_download_bytes`

Backoff is deterministic exponential without jitter. The request timestamp is
updated for every transport attempt, including attempts that fail before
returning a response. `minimum_request_interval_seconds` applies between all
actual request starts; deterministic exponential backoff also applies after
retryable failures. A retry is never sent earlier than either constraint permits.
Identical inputs and fake-clock state produce identical sleep sequences. Tests
inject transport, clock, monotonic clock, and sleeper. Concurrent downloads are
not used.

### HTTP metadata

Live retrieval retains typed HTTP metadata for the acquisition and manifest:

- status
- Content-Type
- Content-Length when valid
- ETag
- Last-Modified
- final validated URL
- whether network retrieval occurred (`network_retrieved`)

Cached raw execution records that no HTTP request occurred, uses null HTTP
response fields, and retains the fixed canonical source URL separately.

### Raw storage

Downloaded bytes are stored content-addressed under the configured raw directory:

```text
storage/raw/football-data-co-uk/sha256/<aa>/<sha256>.csv
```

Writes are atomic (temp file, fsync, rename). Existing matching content is reused
after checksum verification. Symlinks and non-regular files are rejected.
Manifests store relative paths only.

Optional job payload field `raw_sha256` reprocesses a cached artifact without
HTTP.

### CSV decoding and drift

Decoding candidates:

1. UTF-8 with optional BOM
2. CP1252 only when UTF-8 fails

Required columns: `Div`, `Date`, `HomeTeam`, `AwayTeam`, `FTHG`, `FTAG`, `FTR`.

Unknown headers are retained in the raw file, listed in the manifest as
`unknown_source_columns`, and not mapped into dynamic Parquet columns. Absent
optional headers are listed as `missing_optional_source_columns`. Every non-empty
row's `Div` must match the catalog division.

### Canonical and source identity produced by this adapter

Each ingested row produces both identities:

- canonical participant IDs derived from sport, participant type,
  `participant_identity_scope`, and normalized name, never from `source_name`,
  `competition_id`, season, or membership;
- canonical event IDs derived from sport, competition, season, canonical
  participants, and `event_occurrence_key`, never from `event_date` or kickoff;
- source-scoped participant and event references that always include
  `source_name`, retained for provenance and adapter tracing.

Football club identity scopes are provisional and derived from catalog
`country_code`: England uses `club:england`, and Portugal uses `club:portugal`.
`SourceParticipantReference` retains `competition_id` for source context, but
that context is not part of canonical participant identity.

For Football-Data.co.uk domestic leagues, the occurrence key is
`season-ordered-pair-home-1`: one home fixture per ordered pair per season.
Because Football-Data.co.uk does not provide round or matchday, cups, legs, and
other multi-meeting formats need different occurrence-key policies later.

Participant reconciliation uses policy `participant-reconciliation-v1`; event
reconciliation uses policy `event-reconciliation-v1`. Only exact automatic
matches are produced. Different normalized participant names are not silently
merged as aliases; an explicit approved alias mapping is required to unify them.
Unresolved source events are retained in `source_events` and
`event_reconciliations`, but are excluded from canonical `events`,
`market_quotes`, and `post_match_statistics`. See
[data-contracts.md](data-contracts.md).

### Odds columns mapped into the generic market contract

Football-Data.co.uk publishes historical 1X2 prices only. Those columns are
mapped into the sport-agnostic market contract rather than into a bespoke
`odds_1x2` abstraction, so a future adapter can publish totals, handicaps, period
markets, or player markets through the same `market_quotes` dataset without a new
schema.

Every quote from this source uses the canonical definition
`football.match-result.1x2.full-match` with `participant_scope = event` and
`line_type = none`.

Supported column families are explicit; there is no dynamic column discovery.

| provider_type | provider_id | opening columns | closing columns |
| --- | --- | --- | --- |
| `bookmaker` | `bet365` | `B365H`, `B365D`, `B365A` | `B365CH`, `B365CD`, `B365CA` |
| `bookmaker` | `pinnacle` | `PSH`, `PSD`, `PSA` | `PSCH`, `PSCD`, `PSCA` |
| `source-market-average` | `market-average` | `AvgH`, `AvgD`, `AvgA` | `AvgCH`, `AvgCD`, `AvgCA` |
| `source-market-maximum` | `market-maximum` | `MaxH`, `MaxD`, `MaxA` | `MaxCH`, `MaxCD`, `MaxCA` |

The source exposes no market or selection identifiers, so `source_market_id` and
`source_selection_id` stay null instead of being invented. `market_status` and
`selection_status` are `unknown` for the same reason. `source_field` records the
exact CSV column that produced each price.

### Temporal semantics for this source

- `source_observed_at_utc` is when this application observed the CSV; it is never
  reused as a quote time;
- `quoted_at_utc` is null, because the source publishes no original quote
  timestamp;
- `quote_timestamp_precision` is `snapshot-observation-only`;
- `quote_phase` is `opening` or `closing` from the column family, and the two
  phases are never merged;
- no validity window is supplied, so `quote_valid_from_utc` and
  `quote_valid_to_utc` are null;
- HTTP `Last-Modified` stays retrieval metadata in the manifest and never becomes
  a quote time.

With `snapshot-observation-only` precision the contract does not assert that a
price was available at the observation instant. Any future backtest must respect
that limit. See the backtesting implication in
[data-contracts.md](data-contracts.md).

### Source-quality policy

Policy ID: `football-data-co-uk-policy-v1`

- Pinnacle-derived fields are ingested when valid.
- Events on or after `2025-07-23` mark Pinnacle quotes as
  `quality_status = caution` with a concise reliability-warning reason.
- Earlier Pinnacle quotes use neutral `source-provided` status.
- Source average and maximum columns are preserved as aggregates with
  `quality_status = source-provided-aggregate` and are not recalculated by the
  project.
- The policy ID and caution counts appear in the snapshot manifest.

### Current limitations

- Football-Data.co.uk remains the historical CSV adapter (two competitions);
- Betano/Betclic bookmaker adapters cover pre-match football, basketball, and
  tennis current fixtures/odds only, with an exact initial market map;
- no login, bet placement, CAPTCHA bypass, or arbitrary URL navigation;
- live acquisition may break when provider pages change; offline fixtures prove
  parser behaviour without claiming indefinite live success;
- no cross-source fuzzy resolution: only exact canonical identity matching, with
  no silent alias merge;
- no live-network CI dependency for offline unit tests;
- external website availability is required for real bookmaker downloads.
