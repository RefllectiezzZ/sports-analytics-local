# Immutable Parquet snapshots

Ingestion publishes immutable multi-file Parquet snapshots with a canonical JSON
manifest and SQLite operational metadata. The snapshot layer is sport-agnostic;
football ingestion is its only current caller.

## Sport-agnostic snapshot contracts

The shared snapshot infrastructure receives every domain-specific fact through a
validated specification. It never imports a sport, market, or ingestion package,
and `tests/unit/snapshots/test_import_boundary.py` enforces that boundary both by
static import inspection and by asserting that importing the snapshot package
loads no sport module.

| Contract | Responsibility |
| --- | --- |
| `SnapshotIdentity` | `snapshot_type`, `schema_version`, `source_name`, `source_version`, and validated generic `partition_keys` |
| `DatasetDescriptor` | One expected dataset: name, Parquet filename, Arrow schema, and derived schema fingerprint |
| `SnapshotDatasetSuite` | The complete ordered dataset set plus the primary dataset name |
| `SnapshotSpec` | Everything the service needs for one publication: identity, suite, source URL, policy version, observation time, raw artifact, HTTP metadata, producer versions, `domain_metadata`, `quality_summary`, warnings |
| `SnapshotMetrics` | Generic `row_counts`, file count, byte count, `quality_summary`, warning count |
| `PreparedSnapshot` | A verified temporary snapshot directory awaiting publication |
| `PublishedSnapshot` | The generic publication result |

Partition keys are generic validated key/value pairs. Values must be lowercase
path-safe tokens because they become directory names on every supported
platform. Football passes exactly two: `competition_id` and `season_label`.

`PublishedSnapshot` exposes generic `partition_keys`, `metrics.row_counts`, and
`domain_metadata`. It has no `games_count`, `teams_count`, `odds_quotes_count`,
or `statistics_rows_count` field. The football-shaped result is derived in the
ingestion package, which interprets generic dataset counts by name.

Snapshot suite resolution for verification lives in
`sports_analytics.ingestion.snapshot_specs.resolve_snapshot_suite`, keyed by
`(snapshot_type, schema_version)`. That keeps the sport-to-suite mapping in the
ingestion layer instead of inside shared snapshot code.

## Directory layout

```text
storage/snapshots/football-ingestion/football-canonical-v2/<competition_id>/<YYYY-YYYY>/<snapshot-uuid>/
    competitions.parquet
    seasons.parquet
    participants.parquet
    source_participants.parquet
    events.parquet
    event_reconciliations.parquet
    market_quotes.parquet
    post_match_statistics.parquet
    manifest.json
```

The path segments are, in order, the snapshot type (`football-ingestion`), the
schema version (`football-canonical-v2`), then one directory per ordered
partition value (`competition_id`, then `season_label`), then the snapshot UUID.

`SnapshotRepository.relative_path` points to `manifest.json` relative to the
configured snapshots directory. `row_count` stores the primary dataset count,
which is the `events` row count. Absolute paths are never stored in SQLite or
manifests.

## Manifest

`manifest.json` is deterministic canonical JSON (sorted keys, compact separators,
UTF-8, final newline). Its SHA-256 is stored as
`SnapshotRepository.checksum_sha256`.

Manifest version: `snapshot-manifest-v2`. Verification rejects any other version.

Required top-level keys:

| Key | Content |
| --- | --- |
| `manifest_version` | `snapshot-manifest-v2` |
| `snapshot_id` | Canonical lowercase snapshot UUID |
| `snapshot_type` | For example `football-ingestion` |
| `schema_version` | For example `football-canonical-v2` |
| `source_name` | Source identifier |
| `source_version` | Deduplication identity for the source content |
| `source_policy_version` | Source-quality policy version |
| `source_url` | Fixed canonical source URL |
| `source_observed_at_utc` | Canonical UTC observation timestamp |
| `partition_keys` | Generic partition key/value object |
| `domain_metadata` | Domain facts the shared layer does not interpret |
| `producer_versions` | Adapter, parser, normalizer, and reconciliation versions |
| `raw_artifact` | Relative path, SHA-256, byte count, encoding |
| `http_metadata` | Retrieval metadata including `network_retrieved` |
| `python_version` | Interpreter version that produced the snapshot |
| `pyarrow_version` | PyArrow version that produced the snapshot |
| `schema_fingerprints` | Dataset name to expected schema fingerprint |
| `files` | One entry per dataset: filename, SHA-256, byte count, row count, schema fingerprint |
| `row_counts` | Dataset name to row count, cross-checked against `files` |
| `quality_summary` | Bounded non-negative integer counters |
| `warnings` | Sorted warning strings |
| `generated_snapshot_relative_path` | Relative snapshot directory |

For football ingestion, `domain_metadata` carries `sport_code`, `season_id`,
`source_competition_code`, `source_season_code`, `unknown_source_columns`, and
`missing_optional_source_columns`. `quality_summary` carries
`duplicate_rows_discarded`, `warnings_count`, `pinnacle_caution_quote_count`, and
`unresolved_event_count`.

`http_metadata.network_retrieved` is `false` for cached raw execution; in that
case every HTTP response field must be `null`, and both writing and verification
reject a manifest that records response fields without a network request.

Manifests exclude absolute paths, usernames, hostnames, environment variables,
credentials, cookies, full payloads, full source rows, and stack traces.

Every untrusted manifest value passes through a typed validation layer before
use. Malformed manifests always surface as `SnapshotVerificationError`, never as
`KeyError`, `TypeError`, `ValueError`, `OverflowError`, an Arrow exception, or a
raw JSON decoder exception.

## Lifecycle

Statuses: `building` to `ready`, or `building` to `failed`.

READY snapshots are immutable. The application never overwrites their files.

Publication sequence:

1. obtain or load raw bytes;
2. parse and normalize;
3. write and verify Parquet plus manifest in a temporary directory;
4. open a short `BEGIN IMMEDIATE` SQLite transaction;
5. reuse READY, recover BUILDING, adopt orphan, or create BUILDING plus atomic
   rename plus mark READY plus audit;
6. commit.

HTTP, CSV parsing, Parquet generation, and large checksum scans must not run
inside the publication transaction.

## Source-version deduplication

Migration `0003_snapshot_source_deduplication.sql` adds:

- a unique partial index on active (`building` / `ready`) snapshots sharing
  `(snapshot_type, source_name, source_version, schema_version)`;
- a lookup index on `(source_name, source_version, schema_version, status)`.

There is no newer migration; `0001`, `0002`, and `0003` are the complete set.

Source version shape:

```text
e0:2324:sha256:<64-hex>
```

Identical raw bytes for the same competition and season reuse the same source
version. Failed snapshots release the deduplication key so a replacement can
proceed.

## Recovery and the filesystem/SQLite boundary

| Situation | Behaviour |
| --- | --- |
| No row, no directory | create BUILDING, rename prepared dir, mark READY |
| READY row + directory | verify outside the write transaction, re-check row/version/identity in a short `BEGIN IMMEDIATE`, append reuse audit, discard temp |
| Complete orphan directory, no row | bounded discovery under `snapshot_type/schema_version/<partition values>`; adopt only exact identity matches |
| BUILDING + complete directory | derive the final directory from the existing row path (never the newly prepared UUID); verify outside the write transaction; re-check BUILDING/version/identity; mark READY; discard the new temp |
| BUILDING + missing directory | typed retryable busy error; leave BUILDING unchanged; discard the newly prepared temp; do not create another snapshot row |
| Conflicting unexpected directory | permanent integrity error; never delete or overwrite |
| Multiple identity-matching orphans | permanent integrity error |
| Malformed / symlink orphan candidates | ignored by the deterministic discovery policy (not adopted) |

### BUILDING crash recovery

A retry after a crash may prepare a new random snapshot UUID and therefore a
different temporary path. When a BUILDING row already exists:

1. derive its final directory from the parent of `existing.relative_path`;
2. never locate the crashed publication using `prepared.relative_directory`;
3. validate the existing relative path safely;
4. inspect and verify the existing directory outside a write transaction;
5. enter a short `BEGIN IMMEDIATE` transaction;
6. re-read the same snapshot by id and require unchanged status, version, source
   identity, schema version, and relative path;
7. mark READY, append the audit event, commit;
8. discard the newly prepared temporary snapshot.

### Bounded orphan discovery

Orphan adoption inspects only direct child directories of the exact partition
parent
`football-ingestion/football-canonical-v2/<competition_id>/<season_label>/`.
Candidate names must be canonical UUIDs. Symlinks are not followed. Manifests and
files are verified outside SQLite write transactions.

A candidate may be adopted only when its verified identity matches the requested
ingestion exactly for:

- `manifest_version`
- `snapshot_type`
- `schema_version`
- `source_name`
- `source_version`
- every generic partition key and value
- raw artifact checksum
- the expected dataset suite, in declared order

A different manifest checksum from the newly prepared manifest is not automatic
corruption, because snapshot UUID, environment metadata, or supported PyArrow
version may differ. Adoption still requires complete identity and file
verification. Snapshot ID, checksum, row counts, file counts, quality summary,
source observation time, partition keys, and domain metadata are taken from the
verified orphan manifest; prepared identity is never mixed with unrelated
on-disk values.

After verification, a short `BEGIN IMMEDIATE` transaction re-checks that no
active snapshot exists for the source identity, creates BUILDING metadata from
the verified orphan, marks READY, appends audit, and commits. The newly prepared
temporary directory is discarded.

### Transaction boundary

Expensive verification never runs while holding the SQLite writer lock:

1. read candidate metadata with a read-only or ordinary short connection;
2. close the connection;
3. verify filesystem content completely;
4. open `BEGIN IMMEDIATE`;
5. re-read the exact row and expected version/state;
6. perform only short metadata operations, a same-filesystem atomic rename when
   required, and audit insertion;
7. commit.

Race safety relies on the migration-0003 active-source uniqueness, row version
checks, and exact status/identity re-checks.

### Prepared-directory ownership

`FootballIngestionService` owns cleanup of `PreparedSnapshot` until publication
transfers ownership. Temporary prepared directories are removed on checkpoint or
publication failure, READY reuse, and BUILDING/orphan recovery. Successful new
publication retains the renamed final directory. Cleanup failure never replaces a
primary exception; cleanup failure after an otherwise successful
non-publication path is reported deliberately.

Temporary cleanup failures must not replace the primary error. Final immutable
directories are never recursively deleted by publication failure handling.

### Current BUILDING-without-directory limitation

The implementation does not infer worker death from wall-clock age alone. A
BUILDING row without a final directory returns a typed retryable busy error.
Explicit stale-building ownership remains future work.

## Verification

`scraper.py --verify-snapshot <UUID>` and the read-only verifier:

- resolve the recorded `(snapshot_type, schema_version)` pair to its frozen
  dataset suite through `resolve_snapshot_suite`, failing with a typed
  verification error for an unsupported contract;
- resolve the manifest safely under the snapshots root;
- reject traversal and symlinks where relevant;
- verify the manifest checksum against SQLite;
- verify the expected file set, checksums, sizes, Arrow schemas, schema
  fingerprints, and per-dataset row counts;
- confirm the primary dataset (`events`) row count equals repository
  `row_count`;
- never mutate or repair a READY snapshot.

Corruption is reported, not fixed. The CLI prints per-dataset row counts and
returns exit code `2` for verification failures without a traceback.

## At-least-once semantics

- Jobs may run again after a crash.
- Raw storage is content-addressed.
- Active snapshot uniqueness prevents duplicate active snapshots for one source
  version.
- READY reuse makes reprocessing idempotent at the snapshot level.
- HTTP retrieval may occur more than once after an ambiguous crash.
- The project does not claim exactly-once job execution.

## Implementation status

Implemented now: generic snapshot specifications and dataset suites; the
enforced snapshot/sport import boundary; deterministic Parquet writing;
`snapshot-manifest-v2` construction and hostile-input validation; publication,
READY reuse, BUILDING recovery, and bounded orphan adoption; snapshot listing and
verification through `scraper.py`; the football ingestion suite as the only
registered snapshot contract.

Not implemented: additional sports or snapshot types; feature, model, prediction,
or backtesting snapshots; snapshot compaction, retention, or garbage collection;
stale-BUILDING ownership takeover.
