-- Bookmaker acquisition operational metadata (PR #11).
-- Forward-only migration. Do not include BEGIN/COMMIT/ROLLBACK/VACUUM/ATTACH/PRAGMA.
-- Stores run/status/scheduler/fallback evidence only; no raw HTML, odds datasets,
-- secrets, or absolute filesystem paths.

CREATE TABLE bookmaker_acquisition_runs (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    sport TEXT NOT NULL,
    acquisition_cycle_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('succeeded', 'blocked', 'failed')
    ),
    observed_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    snapshot_id TEXT,
    block_reason TEXT,
    failure_classification TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    UNIQUE (provider_id, acquisition_cycle_id, sport)
);

CREATE INDEX idx_bookmaker_acquisition_runs_provider_observed
ON bookmaker_acquisition_runs (provider_id, observed_at, id);

CREATE TABLE bookmaker_acquisition_attempts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES bookmaker_acquisition_runs(id),
    attempt_number INTEGER NOT NULL CHECK (
        typeof(attempt_number) = 'integer' AND attempt_number >= 1
    ),
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (
        outcome IN ('succeeded', 'blocked', 'failed', 'retryable')
    ),
    failure_classification TEXT,
    detail_code TEXT,
    UNIQUE (run_id, attempt_number)
);

CREATE TABLE bookmaker_provider_status (
    provider_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (
        status IN ('healthy', 'blocked', 'degraded', 'unknown')
    ),
    last_attempted_at TEXT,
    last_successful_at TEXT,
    last_valid_snapshot_id TEXT,
    snapshot_age_seconds INTEGER CHECK (
        snapshot_age_seconds IS NULL
        OR (typeof(snapshot_age_seconds) = 'integer' AND snapshot_age_seconds >= 0)
    ),
    events_observed INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(events_observed) = 'integer' AND events_observed >= 0
    ),
    valid_quotes_observed INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(valid_quotes_observed) = 'integer' AND valid_quotes_observed >= 0
    ),
    unresolved_events INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(unresolved_events) = 'integer' AND unresolved_events >= 0
    ),
    rejected_markets INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(rejected_markets) = 'integer' AND rejected_markets >= 0
    ),
    warnings_json TEXT NOT NULL,
    block_failure_classification TEXT,
    next_eligible_at TEXT,
    adapter_version TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE bookmaker_snapshot_registrations (
    snapshot_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    sport TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    checksum_sha256 TEXT NOT NULL CHECK (
        length(checksum_sha256) = 64
        AND checksum_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    relative_path TEXT NOT NULL UNIQUE,
    observed_at TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    acquisition_cycle_id TEXT NOT NULL,
    UNIQUE (provider_id, sport, acquisition_cycle_id)
);

CREATE INDEX idx_bookmaker_snapshot_registrations_provider_observed
ON bookmaker_snapshot_registrations (provider_id, observed_at, snapshot_id);

CREATE TABLE bookmaker_scheduler_cycles (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    sport TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    enqueued_at TEXT NOT NULL,
    job_id TEXT NOT NULL,
    suppressed_duplicate INTEGER NOT NULL CHECK (
        typeof(suppressed_duplicate) = 'integer'
        AND suppressed_duplicate IN (0, 1)
    ),
    UNIQUE (provider_id, sport, scheduled_for)
);

CREATE INDEX idx_bookmaker_scheduler_cycles_provider_scheduled
ON bookmaker_scheduler_cycles (provider_id, scheduled_for, id);

CREATE TABLE bookmaker_fallback_decisions (
    id TEXT PRIMARY KEY,
    preferred_provider TEXT NOT NULL,
    selected_provider TEXT,
    cached_used INTEGER NOT NULL CHECK (
        typeof(cached_used) = 'integer' AND cached_used IN (0, 1)
    ),
    cached_age_seconds INTEGER CHECK (
        cached_age_seconds IS NULL
        OR (typeof(cached_age_seconds) = 'integer' AND cached_age_seconds >= 0)
    ),
    reason_code TEXT NOT NULL,
    attempted_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_bookmaker_fallback_decisions_created
ON bookmaker_fallback_decisions (created_at, id);

CREATE TABLE bookmaker_drift_findings (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    run_id TEXT REFERENCES bookmaker_acquisition_runs(id),
    code TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (
        severity IN ('info', 'warning', 'error')
    ),
    message TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE INDEX idx_bookmaker_drift_findings_provider_observed
ON bookmaker_drift_findings (provider_id, observed_at, id);
