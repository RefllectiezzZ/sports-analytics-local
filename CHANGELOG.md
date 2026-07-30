# Changelog

All notable local releases are documented here.

## 1.0.0 — 2026-07-30

- Added the installed `sports-analytics-v1` operator command and root
  `launch_v1.py` compatibility entry point.
- Added idempotent initialization through exact migrations `0001`–`0005`.
- Added deterministic, side-effect-free release doctor output.
- Extended the existing supervisor to run the package-native durable worker,
  loopback-only Streamlit website, and the bookmaker scheduler only when
  explicitly enabled.
- Added a read-only v1 landing/status view with truthful model probability, fair
  odds, real offered odds, economic hold, and manual-proposal terminology.
- Removed the artifact-writing policy form from the Streamlit product page.
- Added the `sports-analytics-local-backup-v1` checksum-manifest backup and
  fail-closed empty-state restore format.
- Added wheel build/install smoke validation and a bounded localhost Streamlit
  health test.

This release does not add automatic betting, public hosting, login, credentials,
cloud services, profitability claims, or production bookmaker connectivity.
