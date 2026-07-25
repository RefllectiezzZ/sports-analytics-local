# Data contracts

Canonical analytical contracts for `sports-analytics-local`. The contracts are
sport-agnostic; football is the only sport with a production ingestion adapter in
this release.

## Contract versions

| Identifier | Meaning |
| --- | --- |
| `football-canonical-v2` | Canonical schema version for every football Parquet dataset in this release |
| `football-ingestion` | Snapshot type stored in SQLite snapshot metadata |
| `snapshot-manifest-v2` | Manifest document version written and required by verification |
| `football-data-co-uk-adapter-v1` | Football-Data.co.uk ingestion adapter version recorded in manifests |
| `football-data-csv-parser-v1` | Parser implementation version recorded in manifests |
| `football-normalizer-v2` | Normalizer implementation version recorded in manifests |
| `event-reconciliation-v1` | Cross-source event reconciliation policy version |
| `football-data-co-uk-policy-v1` | Source-quality policy version |

Schema versioning policy:

- additive field changes require a new schema version identifier;
- existing READY snapshots remain immutable under their recorded schema version;
- consumers must select snapshots by explicit `schema_version`.

## Identity model

The platform separates four distinct identity concepts. Conflating them is a
contract violation.

| Concept | Contract | Depends on `source_name` |
| --- | --- | --- |
| Canonical participant | `CanonicalParticipant` | No |
| Source participant reference | `SourceParticipantReference` | Yes |
| Canonical event | `CanonicalEvent` | No |
| Source event reference | `SourceEventReference` | Yes |

### Canonical identity

Canonical identifiers are deterministic UUIDv5 values derived **only** from
source-independent facts. They never contain and never depend on `source_name`,
so two different sources describing the same real-world participant or fixture
derive the same canonical identity.

| Identifier | Derived from |
| --- | --- |
| `canonical_participant_id` | `sport_code`, `participant_type`, case-folded `canonical_key` |
| `canonical_event_id` | `sport_code`, `competition_id`, `season_id`, `event_date`, both canonical participant IDs |

Both use a project-owned canonical UUIDv5 namespace that is distinct from the
source namespace, so a source-scoped key can never collide with a canonical key.

### Source identity

Source-scoped identifiers always include `source_name` and exist for provenance
and adapter tracing. They are **not** canonical identities and must never be
described as canonical.

| Identifier | Derived from |
| --- | --- |
| `source_participant_key` | `source_name`, `sport_code`, normalized name |
| `source_participant_id` | UUIDv5 of `source_participant_key` in the source namespace |
| `source_event_key` | `source_name`, `competition_id`, `season_id`, `event_date`, both source participant keys |
| `source_event_id` | UUIDv5 of `source_event_key` in the source namespace |

Every row of the `events` dataset exposes **both** `canonical_event_id` and
`source_event_id`, plus `source_event_key`, `source_name`, and
`source_row_number`, so a canonical fact can always be traced back to the exact
source row that produced it.

Other identifiers:

- competition IDs are stable catalog strings (for example `eng-premier-league`);
- season IDs are `{competition_id}:{YYYY-YYYY}`;
- quote IDs are deterministic UUIDv5 values from canonical event identity, the
  canonical market dimensions, the provider, the phase, and the source field.

## Event reconciliation

Policy version: `event-reconciliation-v1`.

Reconciliation is the auditable, versioned decision that links a source event
reference to a canonical event. Every decision carries an explicit state and a
bounded confidence between `0.0` and `1.0` inclusive.

| State | Confidence | Produced automatically | Meaning |
| --- | --- | --- | --- |
| `exact` | `1.0` | Yes | Every canonical identity component is known and unambiguous, and no candidate contradicts another for the same canonical event |
| `probable` | strictly between `0.0` and `1.0` | No | Reserved by the contract for future scored matching |
| `manual` | strictly between `0.0` and `1.0` | No | Reserved by the contract for an operator-confirmed link |
| `unresolved` | `0.0` | Yes | Identity is incomplete or two candidates conflict |

Only `exact` is produced by an automatic rule in this release. `probable` and
`manual` exist in the contract so later work can add scored matching and an
operator workflow without a schema change.

A candidate becomes `unresolved` when:

- the event date is missing;
- a canonical home or away participant could not be derived;
- the two canonical participants are identical;
- two source events from the same source map ambiguously onto one canonical
  event (duplicate source events);
- two sources report conflicting scheduled start times for one canonical event.

Unresolved events appear **only** in the `event_reconciliations` dataset, with a
null `canonical_event_id` and an explicit reason. They are deliberately excluded
from the `events` dataset and from downstream-safe consumption, so a reader
cannot mistake them for usable candidates. Market quotes and post-match
statistics for an unresolved source event are dropped along with it.

There is no fuzzy name matching, no scored heuristic, and no machine-learning
matcher. Cross-source resolution beyond exact canonical identity is not
implemented.

Downstream-safe states are `exact` and `manual`.

## Datasets

Each football snapshot writes these Parquet files.

| Dataset | File | Purpose |
| --- | --- | --- |
| competitions | `competitions.parquet` | One competition row for the requested catalog entry |
| seasons | `seasons.parquet` | One season row for the requested `YYYY-YYYY` label |
| participants | `participants.parquet` | Canonical, source-independent participants |
| source_participants | `source_participants.parquet` | How this source names each participant |
| events | `events.parquet` | Reconciled canonical events with source references |
| event_reconciliations | `event_reconciliations.parquet` | Every reconciliation decision, including unresolved ones |
| market_quotes | `market_quotes.parquet` | Generic canonical market quotes |
| post_match_statistics | `post_match_statistics.parquet` | Football post-event statistics |

The primary dataset is `events`; its row count is what `SnapshotRepository`
stores as `row_count`. Zero-row Parquet files are allowed for optional datasets
and still carry the full schema.

There is no `teams` dataset, no `games` dataset, and no `odds_1x2` dataset. Those
were replaced by canonical/source participants, canonical events, and the generic
market quote contract respectively.

## Competitions

Required fields: `competition_id`, `sport_code`, `display_name`, `country_code`,
`competition_type`, `source_name`, `source_competition_code`, `timezone`,
`schema_version`. No field is nullable.

Fixed values for this adapter:

- `sport_code = football`
- `source_name = football-data-co-uk`
- `competition_type = domestic-league`

## Seasons

Required fields: `season_id`, `competition_id`, `label`, `start_year`,
`end_year`, `source_season_code`, `schema_version`. No field is nullable.

Canonical input format is `YYYY-YYYY` with consecutive years and no whitespace or
signs. Supported start years are conservatively bounded. Source season codes use
two-digit year pairs (`2023-2024` becomes `2324`).

## Participants and source participants

`participants` holds canonical identity only: `canonical_participant_id`,
`sport_code`, `participant_type`, `canonical_key`, `display_name`,
`schema_version`. No field is nullable, and no field names a source.

`source_participants` holds the source-scoped view:
`source_participant_id`, `source_name`, `source_participant_key`,
`canonical_participant_id`, `participant_type`, `display_name`,
`normalized_name`, `schema_version`. No field is nullable.

`participant_type` is `team` or `player`. Only `team` is produced by the current
adapter.

Display names preserve readable punctuation. Canonical keys are Unicode NFC,
whitespace-collapsed, and case-folded. Empty names, NUL, control characters, and
overlong names are rejected. Distinct display names that collide after
normalization fail ingestion.

## Events

The `events` dataset joins canonical event identity, the source reference that
produced the row, and the reconciliation decision.

Important fields:

- `canonical_event_id` and `source_event_id` (both always present);
- `event_date` (Arrow `date32`);
- `scheduled_start_utc` (UTC timestamp, nullable);
- `start_time_precision`: `date-only`, `minute`, `second`, or `unknown`
  (`date-only` and `minute` are produced by this adapter);
- `status`: one of `scheduled`, `live`, `finished`, `postponed`, `cancelled`,
  `abandoned`, `unknown` (`scheduled` and `finished` are produced by this
  adapter);
- `home_canonical_participant_id` / `away_canonical_participant_id` plus
  `home_source_participant_id` / `away_source_participant_id`;
- full-time scores and `result_code`;
- `outcome_availability_stage`: `post-event` or `pre-event-unavailable`;
- reconciliation columns: `reconciliation_state`, `reconciliation_confidence`,
  `reconciliation_policy_version`;
- provenance: `source_name`, `source_event_key`, `source_row_number`,
  `source_observed_at_utc`, `source_file_sha256`.

Rules:

- both full-time goals empty produces `scheduled` with null scores, null
  `result_code`, and `outcome_availability_stage = pre-event-unavailable`;
- both full-time goals present produces `finished` with
  `outcome_availability_stage = post-event`, and the result must match the score;
- only finished events may carry scores;
- partial scores, negative/non-integral/extreme goals, and identical home/away
  participants are rejected;
- date-only rows leave `scheduled_start_utc` null; no fake kickoff time is
  invented;
- ambiguous DST local times use the earlier occurrence (`fold=0`); nonexistent
  local times are rejected.

Half-time goals and the half-time result are **not** in `events`. They live in
`post_match_statistics`, which keeps post-event information out of the otherwise
pre-match event row.

Ordering: `event_date`, `scheduled_start_utc` (nulls last),
`home_canonical_participant_id`, `away_canonical_participant_id`,
`canonical_event_id`.

## Event reconciliations

Every reconciliation decision is recorded here, resolved and unresolved alike:
`source_name`, `source_event_id`, `source_event_key`, `canonical_event_id`,
`reconciliation_state`, `reconciliation_confidence`,
`reconciliation_policy_version`, `match_key`, `reason`,
`source_observed_at_utc`, `schema_version`.

`canonical_event_id` and `match_key` are null exactly for unresolved
reconciliations. `reason` is null exactly when no problem was recorded.

Ordering: `source_name`, then `source_event_id`.

## Market quotes

The market contract is generic. It is not limited to football 1X2, and it
replaces the previous `odds_1x2` dataset.

Three contracts compose one quote:

| Contract | Responsibility |
| --- | --- |
| `MarketDefinition` | What is being bet on: sport, market family, market key, period, participant scope, line type/value, optional canonical participant |
| `MarketSelection` | One outcome of a definition, plus optional source market/selection identifiers |
| `OddsQuote` | One priced selection observed from one provider at one moment, with explicit temporal and status semantics |

### Market dimensions

| Dimension | Meaning |
| --- | --- |
| `market_family` | Coarse grouping such as `match-result`, `totals`, `handicap` |
| `market_key` | Canonical market type, composed as `{sport}.{family}.{variant}.{period}` |
| `market_period` | Segment of play the market settles on (`full-match`, `first-half`, `set-1`, `map-1`, ...) |
| `participant_scope` | `event`, `home`, `away`, `team`, or `player` |
| `canonical_participant_id` | The competitor a participant-scoped market is about |
| `line_type` | `none`, `total`, `handicap`, or `spread` |
| `line_value` | The handicap or total, absent for outright markets |
| `outcome_key` | The specific selection (`home`, `draw`, `away`, `over`, `under`, `yes`, `no`, ...) |

Market families and keys are **validated extensible canonical strings, not a
closed enum**. A new bookmaker market needs a new canonical string plus the
shared dimensions above; it does not need a new dataset or schema version. The
module publishes documented `KNOWN_MARKET_FAMILIES` and `KNOWN_MARKET_PERIODS`
registries for documentation, tests, and tooling only.

Extensibility is kept safe by enforcing structural rules rather than a closed
vocabulary:

- a `total`, `handicap`, or `spread` market requires a `line_value`;
- a `none` line type must not carry a `line_value`;
- `team`- and `player`-scoped markets require a `canonical_participant_id`;
- `event`-scoped markets must not name a participant;
- line values are bounded and quantized to the canonical line scale;
- decimal odds are bounded (`1.01` to `100000`) and quantized to the canonical
  price scale.

### Provider, status, and quality

| Field | Values |
| --- | --- |
| `provider_type` | `bookmaker`, `exchange`, `source-market-average`, `source-market-maximum` |
| `provider_id` | Validated provider identifier (`bet365`, `pinnacle`, `market-average`, `market-maximum`) |
| `market_status` | `unknown`, `open`, `suspended`, `closed`, `settled` |
| `selection_status` | `unknown`, `active`, `suspended`, `removed` |
| `quality_status` | `source-provided`, `source-provided-aggregate`, `caution` |
| `quality_reason` | Required when `quality_status = caution`, otherwise null |

Football-Data.co.uk publishes no market or selection state, so `market_status`
and `selection_status` are `unknown` rather than an invented value.

### Physical types

- decimal odds use Arrow `decimal128(10, 4)`;
- line values use Arrow `decimal128(8, 2)`;
- timestamps are UTC-normalized Arrow timestamps.

### Implemented markets

Only football full-match 1X2 is emitted by a production adapter:

- `market_family = match-result`
- `market_key = football.match-result.1x2.full-match`
- `market_period = full-match`
- `participant_scope = event`
- `line_type = none`, `line_value` null
- `outcome_key` in `home`, `draw`, `away`

A synthetic totals over/under 2.5 fixture in the test suite proves the contract
generalizes to line markets and that null and non-null line values coexist in one
dataset. No production adapter emits totals.

Partial home/draw/away triples are rejected. Odds at or below `1.01`, non-finite
values, booleans, and comma decimals are rejected.

Ordering: `canonical_event_id`, `market_key`, `market_period`,
`participant_scope`, `line_type`, `line_value`, `provider_type`, `provider_id`,
`quote_phase`, `outcome_key`, `quote_id`.

## Temporal quote semantics

Three different times are recorded separately and are never conflated.

| Field | Meaning |
| --- | --- |
| `source_observed_at_utc` | When this application retrieved or observed the source data |
| `quoted_at_utc` | When the provider's price was published or valid; null when the source publishes no quote time |
| `quote_valid_from_utc` / `quote_valid_to_utc` | Source-supplied validity window; null when the source supplies none |

Two further fields describe how much the recorded times may be trusted.

| Field | Values |
| --- | --- |
| `quote_timestamp_precision` | `exact`, `minute`, `snapshot-observation-only`, `unknown` |
| `quote_phase` | `opening`, `closing`, `current`, `unknown` |

Contract invariants:

- `exact` or `minute` precision requires a non-null `quoted_at_utc`;
- `snapshot-observation-only` precision must not be paired with a
  `quoted_at_utc`;
- `quote_valid_to_utc` must not precede `quote_valid_from_utc`.

For Football-Data.co.uk, `quoted_at_utc` is null and
`quote_timestamp_precision = snapshot-observation-only`. The source labels
opening and closing columns, so `quote_phase` is `opening` or `closing`; the two
phases are never merged. HTTP `Last-Modified` stays retrieval metadata in the
manifest and never becomes a quote time.

### Backtesting implication

A future backtest may use a quote only when its availability relative to
prediction time is supported by the recorded temporal precision and phase. With
`snapshot-observation-only` precision the contract does **not** assert that the
price was available at the observation instant; it asserts only that the
application observed the row at that instant. Treating an
observation-only closing price as available before kickoff would be a leakage
error the contract explicitly refuses to endorse.

Backtesting is not implemented in this release.

## Post-match statistics

Football-specific dataset, emitted only when at least one supported statistic,
referee, or half-time value is present for a finished event.

- `availability_stage = post-match` always;
- half-time goals and the half-time result live here, not in `events`;
- paired home/away counts must both be present or both empty;
- modelling must never treat these fields as pre-match inputs.

Ordering: `canonical_event_id`.

## Forward compatibility for opportunity discovery

The contracts deliberately retain the facts a future bookmaker adapter would
need to support multi-day opportunity discovery:

- event start timestamp and start-time precision;
- event status;
- provider identity (`provider_type`, `provider_id`);
- market status and selection status;
- source observation time;
- original quote time where the source publishes one;
- quote phase;
- validity window where the source supplies one;
- canonical identity plus source identity on every row.

That retained shape is what would later allow multi-day inventory discovery,
manual selection and automatic bet building, multi-sport and multi-date
accumulators, and two distinct odds filters: `selection_odds_range` applied per
selection versus `combined_odds_range` applied to a whole accumulator.

Unknown source information is represented explicitly as null or `unknown` and is
never invented. None of the search, bet-builder, filter, or accumulator
functionality is implemented in this release; only the contract shape that would
permit it exists.

## Leakage boundaries

Post-match statistics and finished-event scores are historical outcomes. Feature
engineering and modelling layers introduced later must respect
`availability_stage`, `outcome_availability_stage`, and event `status` so
post-event information cannot leak into pre-match predictions. Unresolved events
must not be treated as usable candidates.

## Arrow nullability

Schemas use explicit `pa.field(..., nullable=...)` declarations. Required
contract fields are `nullable=False`. Optional fields keep `nullable=True` only
where the contract permits absence, and each schema docstring records why.

| Dataset | Nullable fields | Reason absence is permitted |
| --- | --- | --- |
| competitions | none | every field is required |
| seasons | none | every field is required |
| participants | none | every field is required |
| source_participants | none | every field is required |
| events | `scheduled_start_utc` | the source published only a date, so no kickoff time exists |
| events | `home_score`, `away_score`, `result_code` | the event has not finished, so no outcome exists yet |
| event_reconciliations | `canonical_event_id`, `match_key` | null exactly for unresolved reconciliations |
| event_reconciliations | `reason` | null exactly when no problem was recorded |
| market_quotes | `line_value` | outright markets have no handicap or total |
| market_quotes | `canonical_participant_id` | event-scoped markets are not about one competitor |
| market_quotes | `quoted_at_utc` | the source published no original quote timestamp |
| market_quotes | `quote_valid_from_utc`, `quote_valid_to_utc` | the source supplied no validity window |
| market_quotes | `source_market_id`, `source_selection_id`, `source_field` | the source exposes no such identifier or column |
| market_quotes | `quality_reason` | no quality caveat applies |
| post_match_statistics | `half_time_home_goals`, `half_time_away_goals`, `half_time_result` | the source published no half-time values |
| post_match_statistics | `referee` and every statistic count column | the source publishes statistics inconsistently across seasons |

Everything not listed above is `nullable=False`. In particular every identifier,
`sport_code`, `schema_version`, `source_name`, `source_file_sha256`,
`source_observed_at_utc`, `event_date`, `start_time_precision`, `status`,
`outcome_availability_stage`, the three reconciliation columns, the market
dimension columns (`market_family`, `market_key`, `market_period`,
`participant_scope`, `line_type`, `outcome_key`), `decimal_odds`, `quote_phase`,
`quote_timestamp_precision`, `market_status`, `selection_status`,
`quality_status`, and `availability_stage` are required.

`schema_fingerprint` includes field name, order, type, and nullability. Changing
nullability changes the logical fingerprint but does not require a SQLite
migration.

## Current schema fingerprints (`football-canonical-v2`)

| Dataset | Fingerprint |
| --- | --- |
| competitions | `176b0c72d1e9540ac39bf3b6f784c7ceb87fb01b61b0274968eb636e1d18c43d` |
| seasons | `405d7ae31d3ac696a1665a953d03c5d90d5c2964ecb0807626349f4147279c37` |
| participants | `3bb3a2b833becebb56d9d348a056e86d3b69ebdf00674d4f5645ec123bb4e84f` |
| source_participants | `57eb0cf8235d231bd22372f2aa7549c11a40670e1fdf3114da4db713b4ca2779` |
| events | `61814f1ee0ce462a35ff6a458666a087f65f169f274b5a5a4136f9dde5748b4b` |
| event_reconciliations | `526bdd5ea0977488f28d201b15043378d2a6b4f8354fdc51b2182d71ebe40d7b` |
| market_quotes | `c09df9c559df6e44e1b013d687d630109ee0c5fbcaa940a87d34702f80c8176f` |
| post_match_statistics | `ac215e601f6ba9af7ae5b36459dfd315b86865b48337ccd4a8c29a9527a037bc` |

## Implementation status

Implemented now:

- one Football-Data.co.uk ingestion adapter;
- two football competitions (`eng-premier-league`, `prt-primeira-liga`);
- strict CSV parsing;
- content-addressed raw storage;
- canonical participant and event contracts with source references;
- conservative reconciliation (`exact` and `unresolved` only);
- the generic canonical market quote contract;
- historical 1X2 mapped into that contract;
- immutable generic Parquet snapshots;
- worker job integration (`ingest.football-data-csv` in the frozen default
  registry);
- snapshot listing and verification through `scraper.py`.

Not implemented: Betclic; Betano; current bookmaker prices; browser scraping or
automation; additional sports; markets beyond production 1X2 plus the synthetic
contract proof; models; features; predictions; combinations and accumulators;
backtesting; settlement; bankroll; Streamlit UI; opportunity search engine;
automatic bet builder; user bet filters; cross-source fuzzy resolution.
