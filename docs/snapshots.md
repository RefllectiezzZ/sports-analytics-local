# Immutable Parquet snapshots

Football ingestion publishes immutable multi-file Parquet snapshots with a
canonical JSON manifest and SQLite operational metadata.

## Directory layout

```text
storage/snapshots/
└── football-ingestion/
    └── football-canonical-v1/
        └── <competition_id>/
            └── <YYYY-YYYY>/
                └── <snapshot-uuid>/
                    ├── competitions.parquet
                    ├── seasons.parquet
                    ├── teams.parquet
                    ├── games.parquet
                    ├── odds_1x2.parquet
                    ├── post_match_statistics.parquet
                    └── manifest.json
```

`SnapshotRepository.relative_path` points to `manifest.json` relative to the
configured snapshots directory. `row_count` stores the canonical games count.
Absolute paths are never stored in SQLite or manifests.

## Manifest

`manifest.json` is deterministic canonical JSON (sorted keys, compact
separators, UTF-8, final newline). Its SHA-256 is stored as
`SnapshotRepository.checksum_sha256`.

It records identity, schema fingerprints, source version, raw artifact metadata,
HTTP metadata when applicable, parser/normalizer versions, Python/PyArrow
versions, per-file checksums/sizes/row counts, unknown/missing columns,
warnings, quality summary, and Pinnacle caution counts.

It excludes absolute paths, usernames, hostnames, environment variables,
credentials, cookies, full payloads, full source rows, and stack traces.

## Lifecycle

Statuses: `building` → `ready`, or `building` → `failed`.

READY snapshots are immutable. The application never overwrites their files.

Publication sequence:

1. obtain/load raw bytes;
2. parse and normalize;
3. write and verify Parquet + manifest in a temporary directory;
4. open a short `BEGIN IMMEDIATE` SQLite transaction;
5. reuse READY, recover BUILDING, adopt orphan, or create BUILDING + atomic
   rename + mark READY + audit;
6. commit.

HTTP, CSV parsing, Parquet generation, and large checksum scans must not run
inside the publication transaction.

## Source-version deduplication

Migration `0003_snapshot_source_deduplication.sql` adds:

- unique partial index on active (`building`/`ready`) snapshots sharing
  `(snapshot_type, source_name, source_version, schema_version)`;
- lookup index on `(source_name, source_version, schema_version, status)`.

Source version shape:

```text
e0:2324:sha256:<64-hex>
```

Identical raw bytes for the same competition/season reuse the same source
version. Failed snapshots release the deduplication key so a replacement can
proceed.

## Recovery and the filesystem/SQLite boundary

| Situation | Behaviour |
| --- | --- |
| No row, no directory | create BUILDING, rename prepared dir, mark READY |
| READY row + directory | verify outside the write transaction, re-check row/version/identity in a short `BEGIN IMMEDIATE`, append reuse audit, discard temp |
| Complete orphan directory, no row | bounded discovery under `snapshot_type/schema_version/competition_id/season_label`; adopt only exact identity matches |
| BUILDING + complete directory | derive the final directory from the existing row path (never the newly prepared UUID); verify outside the write transaction; re-check BUILDING/version/identity; mark READY; discard the new temp |
| BUILDING + missing directory | typed retryable busy error; leave BUILDING unchanged; discard the newly prepared temp; do not create another snapshot row |
| Conflicting unexpected directory | permanent integrity error; never delete/overwrite |
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
6. re-read the same snapshot by id and require unchanged status, version,
   source identity, schema version, and relative path;
7. mark READY, append the audit event, commit;
8. discard the newly prepared temporary snapshot.

### Bounded orphan discovery

Orphan adoption inspects only direct child directories of the exact parent
`football-ingestion/football-canonical-v1/<competition_id>/<season_label>/`.
Candidate names must be canonical UUIDs. Symlinks are not followed. Manifests
and files are verified outside SQLite write transactions.

A candidate may be adopted only when its verified identity matches the
requested ingestion exactly for:

- `manifest_version`
- `snapshot_type`
- `schema_version`
- `source_name`
- `source_version`
- `source_competition_code`
- `source_season_code`
- `competition_id`
- `season_id`
- raw artifact checksum
- canonical expected file set

A different manifest checksum from the newly prepared manifest is not automatic
corruption, because snapshot UUID, environment metadata, or supported PyArrow
version may differ. Adoption still requires complete identity and file
verification. Snapshot ID, checksum, row counts, file counts, quality summary,
source observation time, and metadata are taken from the verified orphan
manifest; prepared identity is never mixed with unrelated on-disk values.

After verification, a short `BEGIN IMMEDIATE` transaction re-checks that no
active snapshot exists for the source identity, creates BUILDING metadata from
the verified orphan, marks READY, appends audit, and commits. The newly
prepared temporary directory is discarded.

### Transaction boundary

Expensive verification never runs while holding the SQLite writer lock:

1. read candidate metadata with a read-only or ordinary short connection;
2. close the connection;
3. verify filesystem content completely;
4. open `BEGIN IMMEDIATE`;
5. re-read the exact row and expected version/state;
6. perform only short metadata operations, same-filesystem atomic rename when
   required, and audit insertion;
7. commit.

Race safety relies on migration-0003 active-source uniqueness, row version
checks, and exact status/identity re-checks.

### Prepared-directory ownership

`FootballIngestionService` owns cleanup of `PreparedSnapshot` until publication
transfers ownership. Temporary prepared directories are removed on checkpoint or
publication failure, READY reuse, and BUILDING/orphan recovery. Successful new
publication retains the renamed final directory. Cleanup failure never replaces
a primary exception; cleanup failure after an otherwise successful
non-publication path is reported deliberately.

Temporary cleanup failures must not replace the primary error. Final immutable
directories are never recursively deleted by publication failure handling.

### Current BUILDING-without-directory limitation

The implementation does not infer worker death from wall-clock age alone. A
BUILDING row without a final directory returns a typed retryable busy error. A
future maintenance PR may add explicit stale-building ownership.

## Verification

`scraper.py --verify-snapshot <UUID>` and the read-only verifier:

- resolve the manifest safely under the snapshots root;
- reject traversal and symlinks where relevant;
- verify manifest checksum against SQLite;
- verify expected file set, checksums, sizes, schemas, and row counts;
- confirm games count equals repository `row_count`;
- never mutate or “repair” a READY snapshot.

Corruption is reported, not fixed.

## At-least-once semantics

- Jobs may run again after a crash.
- Raw storage is content-addressed.
- Active snapshot uniqueness prevents duplicate active snapshots for one source
  version.
- READY reuse makes reprocessing idempotent at the snapshot level.
- HTTP retrieval may occur more than once after an ambiguous crash.
- The project does not claim exactly-once job execution.
