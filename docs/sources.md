# External sources

Source adapters retrieve permitted public sports data into content-addressed raw
storage and feed canonical snapshot ingestion.

## Football-Data.co.uk

Source identifier: `football-data-co-uk`

This release implements one adapter. Production ingestion constructs CSV URLs
from a static project-owned catalog. It does **not**:

- scrape HTML pages at runtime;
- launch a browser;
- accept arbitrary URLs, hosts, division codes, or local paths from job payloads;
- require an API key.

Users are responsible for reviewing and respecting the source’s current terms
and permitted use. The project does not bypass authentication, paywalls, access
controls, rate limits, robots restrictions, or anti-bot measures.

### Fixed URL construction

```text
https://www.football-data.co.uk/mmz4281/{source_season_code}/{division_code}.csv
```

Example: Premier League 2023-2024 →
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
- redirects must remain HTTPS on the same host
- cross-host, HTTP, file, localhost, and private-network redirects rejected
- response body streamed with a hard maximum (default 25 MiB, max 100 MiB)
- apparent HTML bodies rejected
- cookies are never stored or logged

### Retry and pacing

Configured under `[scraping]`:

- `request_timeout_seconds`
- `maximum_retries`
- `retry_backoff_base_seconds`
- `retry_backoff_max_seconds`
- `minimum_request_interval_seconds`
- `maximum_download_bytes`

Backoff is deterministic exponential without jitter. Tests inject transport,
clock, monotonic clock, and sleeper. Concurrent downloads are not used.

### Raw storage

Downloaded bytes are stored content-addressed under the configured raw directory:

```text
storage/raw/football-data-co-uk/sha256/<aa>/<sha256>.csv
```

Writes are atomic (temp file, fsync, rename). Existing matching content is
reused after checksum verification. Symlinks and non-regular files are rejected.
Manifests store relative paths only.

Optional job payload field `raw_sha256` reprocesses a cached artifact without
HTTP.

### CSV decoding and drift

Decoding candidates:

1. UTF-8 with optional BOM
2. CP1252 only when UTF-8 fails

Required columns: `Div`, `Date`, `HomeTeam`, `AwayTeam`, `FTHG`, `FTAG`, `FTR`.

Unknown headers are retained in the raw file, listed in the manifest, and not
mapped into dynamic Parquet columns. Every non-empty row’s `Div` must match the
catalog division.

### Source-quality policy

Policy ID: `football-data-co-uk-policy-v1`

- Pinnacle-derived fields are ingested when valid.
- Events on or after `2025-07-23` mark Pinnacle quotes as `quality_status=caution`
  with a concise reliability-warning reason.
- Earlier Pinnacle quotes use neutral `source-provided` status.
- Source average/maximum columns are preserved as aggregates and are not
  recalculated by the project.
- The policy ID and caution counts appear in the snapshot manifest.

### Current limitations

- only two competitions;
- only match-result 1X2 odds;
- no HTML scraping or browser automation;
- no live-network CI dependency;
- external website availability is required for real downloads.
