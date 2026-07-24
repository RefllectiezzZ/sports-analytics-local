-- Initial operational schema for sports-analytics-local.
-- Forward-only migration. Do not include BEGIN/COMMIT/ROLLBACK/VACUUM/ATTACH.
-- schema_migrations is created by the migration runner before applying this file.

CREATE TABLE application_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (length(key) > 0)
);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL,
    attempts INTEGER NOT NULL,
    maximum_attempts INTEGER NOT NULL,
    available_at TEXT NOT NULL,
    lease_owner TEXT NULL,
    lease_expires_at TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT NULL,
    finished_at TEXT NULL,
    last_error TEXT NULL,
    idempotency_key TEXT NULL,
    result_json TEXT NULL,
    version INTEGER NOT NULL,
    CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
    CHECK (typeof(priority) = 'integer'),
    CHECK (typeof(attempts) = 'integer'),
    CHECK (typeof(maximum_attempts) = 'integer'),
    CHECK (typeof(version) = 'integer'),
    CHECK (attempts >= 0),
    CHECK (maximum_attempts > 0),
    CHECK (attempts <= maximum_attempts),
    CHECK (version >= 1),
    CHECK (
        (lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CHECK (
        status NOT IN ('running', 'succeeded', 'failed')
        OR started_at IS NOT NULL
    ),
    CHECK (
        status NOT IN ('succeeded', 'failed', 'cancelled')
        OR finished_at IS NOT NULL
    ),
    CHECK (
        status != 'pending'
        OR (lease_owner IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK (
        status NOT IN ('succeeded', 'failed', 'cancelled')
        OR (lease_owner IS NULL AND lease_expires_at IS NULL)
    )
);

CREATE UNIQUE INDEX uq_jobs_idempotency_key
ON jobs(idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE INDEX idx_jobs_pending_order
ON jobs(priority ASC, available_at ASC, created_at ASC, id ASC)
WHERE status = 'pending';

CREATE INDEX idx_jobs_status_type
ON jobs(status, job_type);

CREATE TABLE job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_status TEXT NULL,
    to_status TEXT NULL,
    details_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    job_version INTEGER NOT NULL,
    CHECK (length(event_type) > 0),
    CHECK (length(actor) > 0),
    CHECK (typeof(job_version) = 'integer'),
    CHECK (job_version >= 1),
    CHECK (
        from_status IS NULL
        OR from_status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')
    ),
    CHECK (
        to_status IS NULL
        OR to_status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')
    ),
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE INDEX idx_job_events_job_occurred
ON job_events(job_id, occurred_at);

CREATE TABLE snapshots (
    id TEXT PRIMARY KEY,
    snapshot_type TEXT NOT NULL,
    status TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    checksum_sha256 TEXT NULL,
    row_count INTEGER NULL,
    source_name TEXT NOT NULL,
    source_version TEXT NULL,
    schema_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ready_at TEXT NULL,
    metadata_json TEXT NOT NULL,
    version INTEGER NOT NULL,
    CHECK (status IN ('building', 'ready', 'failed')),
    CHECK (length(relative_path) > 0),
    CHECK (instr(relative_path, '\') = 0),
    CHECK (substr(relative_path, 1, 1) != '/'),
    CHECK (substr(relative_path, -1) != '/'),
    CHECK (instr(relative_path, '//') = 0),
    CHECK (instr('/' || relative_path || '/', '/../') = 0),
    CHECK (instr('/' || relative_path || '/', '/./') = 0),
    CHECK (row_count IS NULL OR typeof(row_count) = 'integer'),
    CHECK (row_count IS NULL OR row_count >= 0),
    CHECK (typeof(version) = 'integer'),
    CHECK (version >= 1),
    CHECK (
        status != 'ready'
        OR (
            ready_at IS NOT NULL
            AND checksum_sha256 IS NOT NULL
            AND row_count IS NOT NULL
        )
    ),
    CHECK (
        status = 'ready'
        OR ready_at IS NULL
    ),
    CHECK (
        checksum_sha256 IS NULL
        OR (
            length(checksum_sha256) = 64
            AND lower(checksum_sha256) = checksum_sha256
            AND checksum_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    )
);

CREATE UNIQUE INDEX uq_snapshots_relative_path
ON snapshots(relative_path);

CREATE INDEX idx_snapshots_type_status_created
ON snapshots(snapshot_type, status, created_at);

CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NULL,
    actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    correlation_id TEXT NULL,
    details_json TEXT NOT NULL,
    CHECK (length(event_type) > 0),
    CHECK (length(entity_type) > 0),
    CHECK (length(actor) > 0)
);

CREATE INDEX idx_audit_events_occurred
ON audit_events(occurred_at);

CREATE INDEX idx_audit_events_entity
ON audit_events(entity_type, entity_id);

CREATE INDEX idx_audit_events_correlation
ON audit_events(correlation_id)
WHERE correlation_id IS NOT NULL;
