# Local v1 operator runbook

## Boundaries

Sports Analytics Local v1.0.0 is a single-operator, localhost-only application.
The website is read-only. Final placement is manual-only. The supported current
price path is `strict-offline-operator-input`: real offered odds are supplied
through the existing strict CSV/JSON or shared manual validation contracts.
Bookmaker acquisition is disabled by default and is not required for v1.

There is no login, credential generation, CAPTCHA bypass, automatic betting,
staking, bankroll management, public bind address, cloud service, or
profitability claim.

## First run on Windows

From the checkout:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\sports-analytics-v1.exe --initialize
.venv\Scripts\sports-analytics-v1.exe --doctor
.venv\Scripts\sports-analytics-v1.exe
```

Repository compatibility:

```powershell
.venv\Scripts\python.exe launch_v1.py --initialize
.venv\Scripts\python.exe launch_v1.py --doctor
.venv\Scripts\python.exe launch_v1.py
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
- strict offline real offered-price input is the supported price path;
- bookmaker browser acquisition remains optional, disabled by default, and not
  guaranteed;
- no public web hosting, multi-user operation, login, or access control;
- no bet placement, automatic transaction, staking, or bankroll workflow;
- no guarantee of a champion, current odds, economic eligibility, proposal, or
  profit;
- player context remains display-only;
- analytical/audit pages remain read-only and require verified persisted
  artifacts.
