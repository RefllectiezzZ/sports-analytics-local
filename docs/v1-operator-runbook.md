# Local v1 operator runbook

## Boundaries

Sports Analytics Local v1.0.0 is a single-operator, localhost-only application.
The website permits system preparation, automatic allowlisted provider setup,
upcoming-match/current-odds fallback import, and product refresh. Final placement
is manual-only. Automatic mode uses only The Odds API v4 at the exact
`https://api.the-odds-api.com` host. Strict CSV/JSON and editable manual tables
remain Advanced/Fallback paths.

There is no login, credential generation, CAPTCHA bypass, automatic betting,
staking, bankroll management, public bind address, cloud service, or
profitability claim.

## Normal first run from VS Code

1. Open the repository in VS Code.
2. Open **Run and Debug**.
3. Select **Sports Analytics Local MVP**.
4. Press **F5**.
5. Open the printed `http://127.0.0.1:8501` URL.
6. Open **System** and enter the The Odds API key once.
7. Keep region `eu` or choose another allowlisted region, select supported
   competitions, and keep `h2h` unless supported totals are needed.
8. Confirm **Enable automatic operation**.
9. Leave the durable worker running and review automatically refreshed
   risk-adjusted opportunities on **Dashboard** and **Bets**.
10. Place an eligible proposal manually at the bookmaker.

F5 uses the selected repository interpreter and the package-native release
module. Normal launch initializes the runtime idempotently before supervising
the durable worker and Streamlit UI. No regular command-line use is required.
The URL remains loopback-only and the browser is not opened automatically.

Confirmed automatic setup invokes **Prepare system** when needed and is the explicit
operator authorization for governed initial champion preparation. The action
reuses verified compatible artifacts where possible, otherwise runs the
existing verified-historical score tournament and its unchanged production
evidence gates. It strictly reloads the winner and calibration artifacts,
registers the winner as challenger, persists the existing promotion-policy
decision, and applies the audited transition only when that exact decision
authorizes promotion. A hold, retain, reject, incompatible scope, insufficient
history, synthetic provenance, or verification failure leaves no active
bootstrap champion and appears as a blocker in the UI.

Repeated preparation reuses an existing valid champion and does not add another
decision or transition. Merely opening or refreshing Streamlit never runs
training or promotion.

## Guided workspace

- **Dashboard** shows readiness, matches analysed, awaiting odds, analytical
  candidates, held candidates, placeable manual proposals, active competition,
  active model, last successful analysis, automatic connection/quota/sync state,
  top risk-adjusted opportunities, next action, and worker state.
- **Matches** is the Advanced/Fallback fixture workflow and accepts human-friendly
  CSV, JSON, or editable rows. Teams come
  from the verified participant registry; UUIDs, artifact IDs, and checksums
  are derived internally.
- **Odds** is the Advanced/Fallback price workflow and accepts canonical CSV/JSON
  or editable rows for a registered provider, match, market, outcome, optional
  line, decimal odd, and visible UTC timestamp.
  It accepts no URL, credential, cookie, token, selector, or script material.
- **Bets** applies the persisted status/risk/EV/edge/freshness/coverage ranking,
  supports bookmaker/competition/market/risk/status filters, and separates
  ready-for-manual-placement, analytical, held, and rejected rows. It shows
  best and median real prices, price comparison, fair odds, model probability,
  edge, EV, risk tier, exact reasons, and same-bookmaker accumulators.
- **History** summarizes persisted operational state.
- **System** contains one-time masked-key setup, run-now/pause/resume/key
  replacement controls, unresolved reconciliation findings, quota state,
  advanced immutable artifact audit, and allowlisted Football-Data history.

Saving valid matches triggers fair-odds analysis when an active champion
exists. Saving a complete current market automatically validates and publishes
the quotes, runs production inference, calculates probability/edge/EV, builds
singles and eligible accumulators, publishes the read model, and refreshes the
dashboard. Periodic refresh only reads persisted state and never retrains.

Automatic acquisition defaults to every 10 minutes and permits 5 through 60
minutes. Each changed response publishes or reuses immutable provider/event/quote
evidence, runs the existing production football product, and updates the ranked
read model. Identical responses do not duplicate artifacts. Authentication
failure blocks retries until the key is replaced. The quota reserve pauses new
requests. Temporary failures, empty responses, stale quotes, unresolved teams,
and model/economic holds retain the last-known-good product and remain explicit.

## CLI diagnostics and compatibility

The CLI remains available for doctor, explicit initialization, backup, restore,
and advanced diagnostics:

```powershell
.venv\Scripts\sports-analytics-v1.exe --initialize
.venv\Scripts\sports-analytics-v1.exe --doctor
.venv\Scripts\sports-analytics-v1.exe
```

An explicit configuration and dotenv file may be selected with `--config PATH`
and `--env-file PATH`. Initialization validates the complete settings, creates
the configured runtime directories, and applies migrations `0001`–`0005`.
Repeated initialization is safe and does not overwrite configuration, create
`.env`, download a browser, fetch data, train a model, or fabricate analytical
evidence.

## Operate and stop

Normal launch prints `http://127.0.0.1:8501` once. A custom port is accepted
with `--ui-port PORT`; the address remains fixed to `127.0.0.1`. The supervisor
runs the durable worker and package-native Streamlit UI. It also runs the
existing bookmaker scheduler only when `[bookmakers].enabled = true`.

Press `Ctrl+C` in the operator terminal. SIGINT, SIGTERM, and Windows SIGBREAK
request graceful shutdown. If any long-running child exits unexpectedly, the
supervisor stops the others and preserves the first meaningful non-zero status.
`--worker-once` deliberately skips the UI and the long-lived bookmaker
scheduler.

## Interpret doctor

```powershell
.venv\Scripts\sports-analytics-v1.exe --doctor
```

Doctor emits one canonical JSON object and changes no filesystem, database,
logging, cache, or random state.

- `ready` (exit 0): software and required operational state are ready.
- `degraded` (exit 0): the installation is valid but optional analytical data
  such as a champion, current quote, or product read model is absent or held.
- `not-initialized` (exit 3): the configured database does not exist.
- `invalid` (exit 2): configuration, path safety, SQLite integrity, migration,
  queue, or registry inspection has a blocker.

An expected economic-evidence hold is not installation corruption. It means an
analytical candidate remains held or research-only and cannot become a
placeable manual proposal. Fair odds are model estimates. Real offered odds are
external observations. A placeable manual proposal requires both plus every
existing evidence gate.

## Back up

Stop the application, select a new destination, and run:

```powershell
.venv\Scripts\sports-analytics-v1.exe --backup C:\backups\sports-analytics-v1-2026-07-30
```

The `sports-analytics-local-backup-v1` directory contains:

- a consistent SQLite copy made through SQLite's backup API;
- configured raw, snapshots, features, models, and exports state;
- the explicit TOML configuration only when `--config` was supplied and it
  exists;
- a canonical manifest with every relative path, byte size, SHA-256, source
  role, application version, count, and total size.

It excludes `.env`, logs, caches, temporary directories, browser executables,
Git metadata, and credentials. The destination must be new, outside persistent
source state, and free of symlink path components.

## Verify and restore

Restore is both a full manifest verification and a restore operation:

```powershell
.venv\Scripts\sports-analytics-v1.exe --config C:\operator\settings.toml --restore C:\backups\sports-analytics-v1-2026-07-30
.venv\Scripts\sports-analytics-v1.exe --config C:\operator\settings.toml --doctor
```

The configured operational database must be absent. Each configured raw,
snapshots, features, models, and exports destination must be absent or empty.
The command rejects unexpected, missing, altered, duplicate, traversing, or
symlinked files; incompatible versions; non-v1 migration state; and any existing
operational database or non-empty destination. There is no force option.

An included configuration is checksum-verified as backup evidence but is never
allowed to overwrite the operator's current configuration. A failed restore
removes only temporary or newly published restore state and retains no partial
v1 publication.

## Upgrade and rollback

1. Stop the supervisor cleanly.
2. Create and verify a new backup.
3. Record `sports-analytics-v1 --version` and `--doctor` output.
4. Install the intended wheel or checkout version.
5. Run `--initialize` to apply only packaged forward migrations.
6. Run `--doctor`, then launch.

For rollback, stop the new version and retain its state separately. Reinstall
the compatible prior application, configure an absent/empty runtime root, and
restore the prior version's verified backup. v1 never overwrites current state
and does not implement down-migrations.

## Exact v1 limitations

- local machine, one operator, loopback UI only;
- football product scope already implemented before this release;
- one optional external provider, The Odds API v4, supplies configured pre-match
  events and bookmaker quotes; coverage is provider-dependent and not universal;
- API use consumes provider credits; `h2h` is the quota-conserving default;
- provider data can be temporarily absent, stale, unmatched, or quota-paused;
- manual fixture and odds entry remains available as Advanced/Fallback;
- no public web hosting, multi-user operation, login, or access control;
- no bet placement, automatic transaction, staking, or bankroll workflow;
- no guarantee of a champion, current odds, economic eligibility, proposal, or
  profit;
- player context remains display-only;
- analytical/audit pages remain read-only and require verified persisted
  artifacts.

Automatic generation is not bookmaker placement. Economic holds are expected
until prospective evidence exists, and no profitability guarantee is made.
