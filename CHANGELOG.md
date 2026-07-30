# Changelog

All notable local releases are documented here.

## Unreleased — operational local MVP

- Completed confirmed UI-only champion preparation through verified historical
  score tournaments, strict artifact reload, persisted governance decisions,
  and atomic audited promotion; non-promote decisions remain recorded without
  activating a champion.
- Made product-run identity include every semantic operator-quote field,
  including line, scope, settlement rules, validity, and source kind.
- Added the portable **Sports Analytics Local MVP** VS Code F5 launch.
- Made normal `sports-analytics-v1` launch initialize the runtime idempotently
  before starting the durable worker and loopback Streamlit UI.
- Added deterministic MVP readiness orchestration, verified-snapshot
  participant preparation, exact blockers, progress, and fail-closed champion
  handling.
- Added registry-backed Matches and strict current-offered-odds workspaces with
  CSV/JSON upload, editable tables, row errors, duplicate-submit protection,
  and automatic production refresh.
- Reworked the primary UI into Dashboard, Bets, Matches, Odds, History, and
  System, including truthful analytical/held/placeable distinctions and
  same-provider accumulator evidence.
- Added bounded persisted-state refresh and focused MVP/UI/operator integration
  coverage.

Bookmaker login, placement, financial transactions, model-selection bypass,
economic-eligibility overrides, and new migrations remain out of scope.

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
