# Football data contracts

Canonical analytical contracts for football ingestion in
`sports-analytics-local`.

## Versions

| Identifier | Meaning |
| --- | --- |
| `football-canonical-v1` | Canonical schema version for all football Parquet datasets in this release |
| `football-ingestion` | Snapshot type stored in SQLite snapshot metadata |
| `football-data-csv-parser-v1` | Parser implementation version recorded in manifests |
| `football-normalizer-v1` | Normalizer implementation version recorded in manifests |

Schema versioning policy:

- additive field changes require a new schema version identifier;
- existing READY snapshots remain immutable under their recorded schema version;
- consumers must select snapshots by explicit `schema_version`.

## Datasets

Each football snapshot writes these Parquet files:

| Dataset | File | Purpose |
| --- | --- | --- |
| competitions | `competitions.parquet` | One competition row for the requested catalog entry |
| seasons | `seasons.parquet` | One season row for the requested `YYYY-YYYY` label |
| teams | `teams.parquet` | Source-scoped teams observed in the CSV |
| games | `games.parquet` | Canonical matches |
| odds_1x2 | `odds_1x2.parquet` | Match-result 1X2 quotes |
| post_match_statistics | `post_match_statistics.parquet` | Optional post-match counts and referee |

Zero-row Parquet files are allowed for optional datasets and still carry the full
schema.

## Identifiers

- Competition IDs are stable catalog strings (for example `eng-premier-league`).
- Season IDs are `{competition_id}:{YYYY-YYYY}`.
- Team IDs are deterministic UUIDv5 values from `(source_name, normalized team key)`.
- Game IDs are deterministic UUIDv5 values from a canonical source game key.
- Quote IDs are deterministic UUIDv5 values from game, market, selection,
  provider, phase, and source-column family.

The project does **not** claim cross-source entity resolution in this release.

## Competitions

Required fields: `competition_id`, `sport_code`, `display_name`, `country_code`,
`competition_type`, `source_name`, `source_competition_code`, `timezone`,
`schema_version`.

Fixed values for this adapter:

- `sport_code = football`
- `source_name = football-data-co-uk`
- `competition_type = domestic-league`

## Seasons

Canonical input format: `YYYY-YYYY` with consecutive years and no whitespace or
signs. Supported start years are conservatively bounded. Source season codes use
two-digit year pairs (`2023-2024` → `2324`).

## Teams

Display names preserve readable punctuation. Normalized keys are Unicode NFC,
whitespace-collapsed, and case-folded. Empty names, NUL, control characters, and
overlong names are rejected. Distinct display names that collide after
normalization fail ingestion.

## Games

Important fields:

- `event_date` (Arrow date32)
- `scheduled_start_utc` (UTC timestamp, nullable)
- `start_time_precision`: `date-only` or `minute`
- `status`: `scheduled` or `finished`
- full-time and optional half-time goals/results
- provenance: `source_row_number`, `source_observed_at_utc`, `source_file_sha256`

Rules:

- both full-time goals empty → scheduled; scores/results null
- both full-time goals present → finished; result must match score
- partial scores, negative/non-integral/extreme goals, and identical home/away
  teams are rejected
- date-only rows leave `scheduled_start_utc` null; no fake kickoff time is invented
- ambiguous DST local times use the earlier occurrence (`fold=0`); nonexistent
  local times are rejected

Ordering: `event_date`, `scheduled_start_utc` (nulls last), `home_team_id`,
`away_team_id`, `game_id`.

## Odds 1X2

Only the match-result 1X2 market is implemented.

- `market_type = match-result-1x2`
- selections: `home`, `draw`, `away`
- provider types: `bookmaker`, `source-market-average`, `source-market-maximum`
- quote phases: `opening`, `closing` (and not merged)
- decimal odds use Arrow `decimal128(10, 4)`
- `quoted_at_utc` is normally null for this source
- `quote_timestamp_precision = snapshot-observation-only`

Partial H/D/A triples are rejected. Odds ≤ 1, non-finite values, booleans, and
comma decimals are rejected.

## Post-match statistics

Emitted only when at least one supported statistic or referee field is present.

- `availability_stage = post-match` always
- paired home/away counts must both be present or both empty
- modelling must never treat these fields as pre-match inputs

## Source observation versus quote time

Every ingestion receives one explicit `source_observed_at_utc`. That timestamp is
used consistently across games, odds, statistics, manifests, snapshot metadata,
and audit events. HTTP `Last-Modified` is retained as source metadata only and
never replaces observation time.

## Leakage implications

Post-match statistics and finished-game scores are historical outcomes. Feature
engineering and modelling layers introduced in later PRs must respect
`availability_stage` and game status so post-match information cannot leak into
pre-match predictions.
