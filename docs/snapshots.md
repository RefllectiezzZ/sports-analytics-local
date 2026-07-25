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
| READY row + directory | verify and reuse; discard temp |
| Complete directory, no row | verify and adopt; never overwrite |
| BUILDING + complete directory | verify and complete READY |
| BUILDING + missing directory | retryable busy/incomplete error |
| Conflicting unexpected directory | permanent integrity error; never delete/overwrite |

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
