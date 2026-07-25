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
| `participant-reconciliation-v1` | Cross-source participant reconciliation policy version |
| `event-reconciliation-v1` | Cross-source event reconciliation policy version |
| `football-data-co-uk-policy-v1` | Source-quality policy version |

Schema versioning policy:

- additive field changes require a new schema version identifier;
- existing READY snapshots remain immutable under their recorded schema version;
- consumers must select snapshots by explicit `schema_version`.

## Identity model

The platform separates canonical identity, source-scoped provenance, and
reconciliation decisions. Conflating them is a contract violation.

| Concept | Contract | Depends on `source_name` |
| --- | --- | --- |
| Canonical participant | `CanonicalParticipant` | No |
| Source participant reference | `SourceParticipantReference` | Yes |
| Participant reconciliation | `ParticipantReconciliation` | Yes |
| Canonical event | `CanonicalEvent` | No |
| Source event reference | `SourceEventReference` | Yes |
| Event reconciliation | `EventReconciliation` | Yes |

### Canonical identity

Canonical identifiers are deterministic UUIDv5 values derived **only** from
source-independent facts. They never contain and never depend on `source_name`.

| Identifier | Derived from |
| --- | --- |
| `canonical_participant_id` | `sport_code`, `participant_type`, `participant_identity_scope`, case-folded `canonical_key` |
| `canonical_event_id` | `sport_code`, `competition_id`, `season_id`, both canonical participant IDs, `event_occurrence_key` |

Canonical participant IDs are scoped by `participant_identity_scope`, not by
`competition_id`. Participant identity is independent of `source_name`,
`competition_id`, season, and current membership. For football clubs the current
adapter derives provisional scopes from catalog `country_code`:
`club:england` for England and `club:portugal` for Portugal. The same club in a
league and cup inside one association receives the same ID; the same normalized
name in England and Portugal receives different IDs. Player identity uses
`participant_identity_scope` without requiring `competition_id`.

Canonical event IDs use `event_occurrence_key`, not `event_date` or kickoff time.
Dates and scheduled starts are mutable metadata so postponed or rescheduled
fixtures retain one canonical identity. For the current Football-Data.co.uk
domestic-league adapter, the occurrence key is
`season-ordered-pair-home-1`: one home fixture for an ordered home/away pair in a
season. Football-Data.co.uk does not provide round or matchday, so cups, legs,
replays, playoffs, or competitions with multiple same-home meetings will need a
different occurrence key policy later.

Canonical and source identifiers use distinct project-owned UUIDv5 namespaces, so
a source-scoped key can never collide with a canonical key.

### Source identity

Source-scoped identifiers always include `source_name` and exist for provenance
and adapter tracing. They are **not** canonical identities and must never be
described as canonical.

| Identifier | Derived from |
| --- | --- |
| `source_participant_key` | `source_name`, `sport_code`, `competition_id`, normalized name |
| `source_participant_id` | UUIDv5 of `source_participant_key` in the source namespace |
| `source_event_key` | `source_name`, `competition_id`, `season_id`, `event_date`, both source participant keys |
| `source_event_id` | UUIDv5 of `source_event_key` in the source namespace |

`SourceParticipantReference` retains `competition_id` for source context, but
that context does not participate in canonical participant identity.

The `source_events` dataset retains every source event candidate, including
unresolved rows, with row-level provenance. The `events` dataset is canonical
only, unique by `canonical_event_id`, and contains no per-source duplication or
source provenance columns.

Other identifiers:

- competition IDs are stable catalog strings (for example `eng-premier-league`);
- season IDs are `{competition_id}:{YYYY-YYYY}`;
- market quote identity is split between `quote_series_id` and
  `quote_observation_id`.

## Participant reconciliation

Policy version: `participant-reconciliation-v1`.

Participant reconciliation is the auditable, versioned decision that links a
source participant reference to a canonical participant scoped by sport,
participant type, `participant_identity_scope`, and canonical key. Every decision
carries an explicit state and a bounded confidence between `0.0` and `1.0`
inclusive.

| State | Confidence | Produced automatically | Meaning |
| --- | --- | --- | --- |
| `exact` | `1.0` | Yes | Valid normalized name in a participant identity scope resolves to its own canonical key |
| `probable` | strictly between `0.0` and `1.0` | No | Reserved by the contract for future scored matching |
| `manual` | `1.0` | No | Reserved by the contract for an operator-confirmed link |
| `unresolved` | `0.0` | Yes | Identity is incomplete, duplicated, conflicting, or has an explicit alias/equivalence claim without an approved mapping |

Only `exact` is produced by an automatic rule in this release. The
`participant-reconciliation-v1` policy is provisional name-based exact matching:
a valid normalized name in a scope resolves to its own canonical key. Different
normalized names are distinct exact identities unless an explicit supported alias
mapping unifies them. Unknown pairs of different names are not automatically
unresolved aliases, and aliases are not merged silently; for example `Sporting
CP` and `Sporting Lisbon` remain separate unless an approved mapping says
otherwise. An explicit alias or equivalence claim submitted without an approved
mapping may remain unresolved with an auditable reason.

## Event reconciliation

Policy version: `event-reconciliation-v1`.

Event reconciliation is the auditable, versioned decision that links a source
event reference to a canonical event. Every decision carries an explicit state and
a bounded confidence between `0.0` and `1.0` inclusive.

| State | Confidence | Produced automatically | Meaning |
| --- | --- | --- | --- |
| `exact` | `1.0` | Yes | Sport, competition, season, both canonical participants, and `event_occurrence_key` are known and unambiguous |
| `probable` | strictly between `0.0` and `1.0` | No | Reserved by the contract for future scored matching |
| `manual` | `1.0` | No | Reserved by the contract for an operator-confirmed link |
| `unresolved` | `0.0` | Yes | Identity is incomplete or source candidates conflict |

Only `exact` is produced by an automatic rule in this release. Scheduled date and
kickoff are evidence/metadata only; they do not form canonical event identity.

A candidate becomes `unresolved` when:

- `event_occurrence_key` is missing;
- a canonical home or away participant could not be derived;
- the two canonical participants are identical;
- duplicate source events or duplicate occurrence candidates make the mapping
  ambiguous.

Unresolved source events stay in `source_events` and
`event_reconciliations`, with a null `canonical_event_id` and an explicit reason.
They are deliberately excluded from `events`, `market_quotes`, and
`post_match_statistics`, so downstream-safe datasets contain only resolved
canonical facts.

There is no fuzzy name matching, no scored heuristic, and no machine-learning
matcher. Cross-source resolution beyond exact canonical identity is not
implemented. Downstream-safe states are `exact` and `manual`.

When multiple downstream-safe sources map to one canonical event, immutable
identity dimensions must agree. Conflicting finished outcomes raise
`SourceIntegrityError`. Mutable scheduling and status metadata is selected from
the most recent source observation, then by source authority, then by
lexicographic `source_name`. Every original source fact remains in
`source_events`.

## Datasets

Each `football-canonical-v2` snapshot writes these ten Parquet files.

| Dataset | File | Purpose |
| --- | --- | --- |
| competitions | `competitions.parquet` | One competition row for the requested catalog entry |
| seasons | `seasons.parquet` | One season row for the requested `YYYY-YYYY` label |
| participants | `participants.parquet` | Canonical participants scoped by participant identity scope |
| source_participants | `source_participants.parquet` | How each source names each participant, resolved or unresolved |
| participant_reconciliations | `participant_reconciliations.parquet` | Every participant reconciliation decision |
| events | `events.parquet` | Canonical events, unique by `canonical_event_id` |
| source_events | `source_events.parquet` | Every source event candidate with provenance, including unresolved rows |
| event_reconciliations | `event_reconciliations.parquet` | Every event reconciliation decision |
| market_quotes | `market_quotes.parquet` | Generic canonical market quotes for resolved events |
| post_match_statistics | `post_match_statistics.parquet` | Football post-event statistics for resolved events |

The primary dataset is `events`; its row count is what `SnapshotRepository`
stores as `row_count`. Zero-row Parquet files are allowed for optional datasets
and still carry the full schema.

There is no `teams` dataset, no `games` dataset, and no `odds_1x2` dataset. Those
were replaced by canonical/source participants, canonical events/source events,
and the generic market quote contract respectively.

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
`sport_code`, `participant_identity_scope`, `participant_type`, `canonical_key`,
`display_name`, `schema_version`. No field is nullable, and no field names a
source or competition.

`source_participants` holds the source-scoped view:
`source_participant_id`, `source_name`, `source_participant_key`,
`competition_id`, nullable `canonical_participant_id`, `participant_type`,
`display_name`, `normalized_name`, `schema_version`.
`SourceParticipantReference.canonical_participant_id` is nullable until an exact,
probable, or manual reconciliation succeeds; unresolved source participants do
not claim a canonical ID.

`participant_reconciliations` records every source participant decision:
`source_name`, `source_participant_id`, `source_participant_key`,
`canonical_participant_id`, `reconciliation_state`,
`reconciliation_confidence`, `reconciliation_policy_version`, `match_key`,
`reason`, `source_observed_at_utc`, `schema_version`.

`canonical_participant_id` and `match_key` are null exactly for unresolved
participant reconciliations. `reason` is null exactly when no problem was
recorded.

`participant_type` is `team`, `club`, or `player`. The current football adapter
produces `club`.

Display names preserve readable punctuation. Canonical keys are Unicode NFC,
whitespace-collapsed, and case-folded. Empty names, NUL, control characters, and
overlong names are rejected. Distinct display names that collide after
normalization fail ingestion.

## Events

The `events` dataset contains canonical event rows only. It is unique by
`canonical_event_id` and contains no `source_name`, `source_event_id`,
`source_event_key`, row-number, file-hash, or reconciliation columns.

Important fields:

- `canonical_event_id`;
- `sport_code`, `competition_id`, `season_id`;
- `event_occurrence_key`;
- `event_date` (Arrow `date32`);
- `scheduled_start_utc` (UTC timestamp, nullable);
- `start_time_precision`: `date-only`, `minute`, `second`, or `unknown`
  (`date-only` and `minute` are produced by this adapter);
- `status`: one of `scheduled`, `live`, `finished`, `postponed`, `cancelled`,
  `abandoned`, `unknown` (`scheduled` and `finished` are produced by this
  adapter);
- `home_canonical_participant_id` / `away_canonical_participant_id`;
- full-time scores and `result_code`;
- `outcome_availability_stage`: `post-event` or `pre-event-unavailable`.

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

Ordering: `event_date`, `event_occurrence_key`,
`home_canonical_participant_id`, `away_canonical_participant_id`,
`canonical_event_id`.

## Source events

The `source_events` dataset retains every source event candidate, resolved or
unresolved, with the exact provenance needed for audit and replay:
`source_name`, `source_event_id`, `source_event_key`, nullable
`canonical_event_id`, `competition_id`, `season_id`, nullable
`event_occurrence_key`, nullable `event_date`, nullable `scheduled_start_utc`,
`start_time_precision`, `status`, source and canonical home/away participant IDs,
scores, `result_code`, `outcome_availability_stage`, `source_row_number`,
`source_file_sha256`, `source_observed_at_utc`, reconciliation columns,
nullable `reconciliation_reason`, and `schema_version`.

`canonical_event_id`, `event_occurrence_key`, `event_date`, and canonical
participant IDs are nullable so unresolved source rows can be retained without
claiming a canonical identity. Unresolved source rows must not feed
`market_quotes` or `post_match_statistics`.

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

### Quote identity

Quote identity is split deliberately:

| Identifier | Meaning |
| --- | --- |
| `quote_series_id` | Stable UUIDv5 for one canonical event, market selection, and provider |
| `quote_observation_id` | UUIDv5 for one concrete source observation of that series, distinguished by source provenance and time dimensions |

The `market_quotes` schema includes both IDs plus `source_name` and
`source_event_id`, so a resolved quote can be traced to the source event that
produced the observation without making source identity part of the canonical
event.

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
- `decimal_odds` and `line_value` are validated and quantized in frozen
  dataclasses before serialization;
- timestamps are normalized to UTC before serialization as Arrow timestamps.

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
`quote_phase`, `outcome_key`, `quote_observation_id`.

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
- canonical identity plus source identity where the dataset contract permits
  source-specific rows.

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
| source_participants | `canonical_participant_id` | null exactly for unresolved participant references |
| participant_reconciliations | `canonical_participant_id`, `match_key` | null exactly for unresolved reconciliations |
| participant_reconciliations | `reason` | null exactly when no problem was recorded |
| events | `scheduled_start_utc` | the source published only a date, so no kickoff time exists |
| events | `home_score`, `away_score`, `result_code` | the event has not finished, so no outcome exists yet |
| source_events | `canonical_event_id`, `event_occurrence_key`, `event_date`, `home_canonical_participant_id`, `away_canonical_participant_id` | unresolved source rows do not claim complete canonical identity |
| source_events | `scheduled_start_utc` | the source published only a date, or the row is unresolved before scheduling metadata is usable |
| source_events | `home_score`, `away_score`, `result_code` | the event has not finished, or the source row is unresolved |
| source_events | `reconciliation_reason` | null exactly when no problem was recorded |
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

Everything not listed above is `nullable=False`. In particular every required
identifier for each dataset, `sport_code`, `schema_version`, `source_name`,
`source_file_sha256`, `source_observed_at_utc`, required event scheduling/status
fields, `outcome_availability_stage`, the three reconciliation columns, the
market dimension columns (`market_family`, `market_key`, `market_period`,
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
| participants | `c4744a6bf603531abdee573a7d52ed0c565c71f432970fadcc865fe85c9c2ba2` |
| source_participants | `45f11469fe57b11d57fb1fa86dca839816ab8c13a201610966d2d1f69d0a1103` |
| participant_reconciliations | `8dbcd38f873aa54aa72a882832d578f7d286f73bc6a2a606752f48fe7dc16590` |
| events | `927b0e1848798b84d39a243432a7f70bc774582c08db66b1274e00ba31addcb4` |
| source_events | `7c30592e8d79788f7dfd2903fcc9540cb4c77a0317ffb6f5cd439ab4b541968e` |
| event_reconciliations | `526bdd5ea0977488f28d201b15043378d2a6b4f8354fdc51b2182d71ebe40d7b` |
| market_quotes | `42941e0f8cdc107bbed850f42a7ac510a19b93b892844f49d66a84304120c871` |
| post_match_statistics | `ac215e601f6ba9af7ae5b36459dfd315b86865b48337ccd4a8c29a9527a037bc` |

## Implementation status

Implemented now:

- one Football-Data.co.uk ingestion adapter;
- two football competitions (`eng-premier-league`, `prt-primeira-liga`);
- strict CSV parsing;
- content-addressed raw storage;
- participant-identity-scope canonical participant IDs and occurrence-key
  canonical event IDs;
- source participant/event provenance datasets, including unresolved source
  events;
- conservative participant and event reconciliation (`exact` and `unresolved`
  only);
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
