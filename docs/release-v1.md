# Local v1.0.0 release

## Release definition

Version 1.0.0 packages the completed analytical system as a coherent local
operator application. The release boundary is operational hardening, not new
analytical capability.

The installed wheel provides:

```text
sports-analytics-v1 --version
sports-analytics-v1 --initialize
sports-analytics-v1 --doctor
sports-analytics-v1 --backup BACKUP_DIRECTORY
sports-analytics-v1 --restore BACKUP_DIRECTORY
sports-analytics-v1 [--ui-port PORT] [worker options]
```

The installed launch path depends only on packaged modules. Streamlit runs the
packaged `sports_analytics/ui/streamlit_entry.py`, bound to `127.0.0.1`, in
headless mode with usage-stat collection disabled. The worker runs through
`python -m sports_analytics.jobs`. No repository-root script is required.

## Truth and safety boundaries

- Model probability and fair odds are analytical estimates.
- Real offered odds are independently supplied external prices.
- Analytical candidates can remain research-only, held, or rejected.
- Only evidence-eligible rows are described as placeable manual proposals.
- Every placement remains manual.
- Expected economic holds do not imply a broken installation.
- The supported v1 path requires no bookmaker network access.
- There is no profitability, automatic betting, cloud, public hosting,
  production bookmaker connectivity, or multi-user claim.

## Recovery format

`sports-analytics-local-backup-v1` uses a directory manifest rather than an
archive, so restore performs no archive extraction. SHA-256 and byte size cover
the SQLite backup, optional explicit TOML configuration, and every copied
persistent file. Semantic paths and source roles are exact and canonical.
Symlinks, traversal, mutation during copy, unexpected files, and incompatible
database migration state fail closed.

## Release validation

The Windows/Python 3.12 workflow retains dependency checking, the complete test
suite, Ruff lint and formatting, and mypy. It additionally builds a wheel
without dependencies, installs it into a temporary system-site-packages venv,
runs installed `--version` and `--help`, imports the package, and asserts
`__version__ == "1.0.0"`. CI does not publish a package or create a release.

Focused release coverage includes initialization idempotence, read-only doctor
states, supervisor command allowlists, backup/restore checksums and empty-state
round trip, read-only UI controls, version/entry-point consistency, and a
bounded localhost-only Streamlit health endpoint smoke test.

Migrations `0001`–`0005` remain the complete v1 schema and are unchanged.
