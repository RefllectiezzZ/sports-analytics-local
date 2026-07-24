# Architecture

This document describes the **intended** architecture for `sports-analytics-local`.

**None of the application components described below are implemented in the current bootstrap task.** The repository currently provides packaging, placeholders, documentation, and quality tooling only.

## Entry points (planned)

| Component | Role |
| --- | --- |
| `app.py` | Streamlit entry point for local interactive analytics and review. |
| `scraper.py` | Coordinates data ingestion from permitted public sources. |
| `engine.py` | Coordinates feature generation, prediction, and combination generation. |
| `worker.py` | Executes background jobs outside the Streamlit process. |
| `run_local.py` | Coordinates local startup of the processes needed for localhost operation. |

## Storage principles (planned)

- **SQLite** will store operational state, metadata, jobs, predictions, and audit records.
- **Parquet** will store historical and analytical datasets.
- **Snapshots** will be immutable once written.
- Runtime-generated files live under `storage/`; only `.gitkeep` markers are tracked in Git.

## Processing principles (planned)

- Processing must be **deterministic**, including seeded modelling where applicable.
- Features, rules, datasets, and models will be **versioned**.
- Sports models and market models will eventually be **separated**.
- Failures must be **explicit and auditable** (no silent data loss or silent retries without records).

## External dependency boundaries

- External AI, LLM, or cloud inference services will **not** be runtime dependencies.
- Paid APIs will **not** be required for runtime operation.
- Scraping will use **permitted public sources only**.

## Package layout

Application code lives under `src/sports_analytics/` with focused subpackages:

- `core` — shared primitives
- `data` — persistence and dataset I/O
- `scrapers` — ingestion adapters
- `features` — feature engineering
- `models` — local statistical / ML components
- `combinations` — combination generation helpers
- `services` — workflow orchestration
- `evaluation` — evaluation utilities

## Bootstrap status

At bootstrap time:

- entry-point scripts are typed placeholders only;
- no scraper, engine, worker, or Streamlit logic exists;
- no SQLite schemas or Parquet pipelines exist;
- no prediction, betting, or combination business logic exists.
