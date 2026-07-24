-- Worker runtime metadata and queue-integrity enforcement.
-- Forward-only migration. Do not include BEGIN/COMMIT/ROLLBACK/VACUUM/ATTACH/PRAGMA.
-- Migration 0001 is immutable; lease invariants for running jobs are enforced here.

CREATE TABLE worker_instances (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    process_id INTEGER NOT NULL,
    hostname TEXT NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    stopping_at TEXT NULL,
    stopped_at TEXT NULL,
    current_job_id TEXT NULL,
    last_error TEXT NULL,
    capabilities_json TEXT NOT NULL,
    version INTEGER NOT NULL,
    CHECK (length(id) > 0),
    CHECK (length(name) > 0),
    CHECK (length(hostname) > 0),
    CHECK (length(capabilities_json) > 0),
    CHECK (status IN ('starting', 'running', 'stopping', 'stopped', 'failed')),
    CHECK (typeof(process_id) = 'integer'),
    CHECK (process_id > 0),
    CHECK (typeof(version) = 'integer'),
    CHECK (version >= 1),
    CHECK (
        status NOT IN ('starting', 'running')
        OR stopped_at IS NULL
    ),
    CHECK (
        status NOT IN ('stopped', 'failed')
        OR stopped_at IS NOT NULL
    ),
    CHECK (
        status != 'stopping'
        OR stopping_at IS NOT NULL
    ),
    CHECK (
        status NOT IN ('stopped', 'failed')
        OR current_job_id IS NULL
    ),
    FOREIGN KEY (current_job_id) REFERENCES jobs(id)
);

CREATE INDEX idx_worker_instances_status_heartbeat
ON worker_instances(status, heartbeat_at);

CREATE INDEX idx_worker_instances_heartbeat
ON worker_instances(heartbeat_at);

CREATE UNIQUE INDEX uq_worker_instances_current_job
ON worker_instances(current_job_id)
WHERE current_job_id IS NOT NULL;

CREATE INDEX idx_jobs_running_lease_expires
ON jobs(lease_expires_at ASC, updated_at ASC, id ASC)
WHERE status = 'running';

CREATE TRIGGER trg_jobs_running_lease_insert
BEFORE INSERT ON jobs
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NEW.status = 'running'
             AND (NEW.lease_owner IS NULL OR NEW.lease_expires_at IS NULL)
        THEN RAISE(ABORT, 'running job requires complete lease')
        WHEN NEW.status != 'running'
             AND (NEW.lease_owner IS NOT NULL OR NEW.lease_expires_at IS NOT NULL)
        THEN RAISE(ABORT, 'non-running job must not retain a lease')
    END;
END;

CREATE TRIGGER trg_jobs_running_lease_update
BEFORE UPDATE OF status, lease_owner, lease_expires_at ON jobs
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NEW.status = 'running'
             AND (NEW.lease_owner IS NULL OR NEW.lease_expires_at IS NULL)
        THEN RAISE(ABORT, 'running job requires complete lease')
        WHEN NEW.status != 'running'
             AND (NEW.lease_owner IS NOT NULL OR NEW.lease_expires_at IS NOT NULL)
        THEN RAISE(ABORT, 'non-running job must not retain a lease')
    END;
END;

-- Validate every existing jobs row against the new lease invariant.
-- The BEFORE UPDATE trigger rejects legacy running rows with a NULL lease.
UPDATE jobs
SET status = status,
    lease_owner = lease_owner,
    lease_expires_at = lease_expires_at;
