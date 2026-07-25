-- Snapshot source-version deduplication indexes for sports-analytics-local.
-- Forward-only migration. Do not include BEGIN/COMMIT/ROLLBACK/VACUUM/ATTACH.
-- Does not create sports-domain tables; operational metadata only.

CREATE UNIQUE INDEX uq_snapshots_active_source_version
ON snapshots (
    snapshot_type,
    source_name,
    source_version,
    schema_version
)
WHERE source_version IS NOT NULL
  AND status IN ('building', 'ready');

CREATE INDEX idx_snapshots_source_version_status
ON snapshots (
    source_name,
    source_version,
    schema_version,
    status
);
