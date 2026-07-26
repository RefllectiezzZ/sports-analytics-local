CREATE TABLE result_snapshots (
    id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    identity_version TEXT NOT NULL,
    canonical_event_id TEXT NOT NULL,
    sport_code TEXT NOT NULL,
    event_status TEXT NOT NULL CHECK (
        event_status IN (
            'scheduled', 'in-progress', 'completed', 'postponed',
            'cancelled', 'abandoned', 'incomplete'
        )
    ),
    source_name TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    source_observed_at TEXT NOT NULL,
    result_timestamp TEXT,
    checksum_sha256 TEXT NOT NULL CHECK (
        length(checksum_sha256) = 64
        AND checksum_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    relative_path TEXT NOT NULL UNIQUE,
    registered_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    UNIQUE (id, checksum_sha256),
    UNIQUE (source_name, source_event_id, source_observed_at)
);

CREATE INDEX idx_result_snapshots_event_observed
ON result_snapshots (canonical_event_id, source_observed_at, id);

CREATE TABLE settlement_runs (
    id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    source_artifact_id TEXT NOT NULL,
    source_artifact_checksum_sha256 TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    as_of_utc TEXT NOT NULL,
    report_relative_path TEXT NOT NULL,
    report_checksum_sha256 TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (
        source_artifact_id, source_artifact_checksum_sha256,
        policy_id, policy_version, as_of_utc
    )
);

CREATE TABLE analytical_settlements (
    id TEXT PRIMARY KEY,
    settlement_version TEXT NOT NULL,
    settlement_run_id TEXT NOT NULL REFERENCES settlement_runs(id),
    position_type TEXT NOT NULL CHECK (position_type IN ('single', 'combination')),
    position_id TEXT NOT NULL,
    source_artifact_id TEXT NOT NULL,
    source_artifact_checksum_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'win', 'loss', 'push', 'void', 'unresolved')
    ),
    decimal_odds TEXT NOT NULL,
    stake_units TEXT NOT NULL,
    returned_units TEXT NOT NULL,
    profit_units TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    as_of_utc TEXT NOT NULL,
    evidence_fingerprint TEXT NOT NULL,
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (settlement_run_id, position_type, position_id)
);

CREATE TABLE current_analytical_settlements (
    source_artifact_id TEXT NOT NULL,
    position_type TEXT NOT NULL CHECK (position_type IN ('single', 'combination')),
    position_id TEXT NOT NULL,
    settlement_id TEXT NOT NULL UNIQUE REFERENCES analytical_settlements(id),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'win', 'loss', 'push', 'void', 'unresolved')
    ),
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (
        typeof(version) = 'integer' AND version >= 1
    ),
    PRIMARY KEY (source_artifact_id, position_type, position_id)
);

CREATE INDEX idx_current_analytical_settlements_status
ON current_analytical_settlements (status, updated_at, settlement_id);

CREATE TABLE settlement_evidence (
    settlement_id TEXT NOT NULL REFERENCES analytical_settlements(id),
    opportunity_id TEXT NOT NULL,
    canonical_event_id TEXT NOT NULL,
    result_snapshot_id TEXT NOT NULL REFERENCES result_snapshots(id),
    result_checksum_sha256 TEXT NOT NULL,
    PRIMARY KEY (settlement_id, opportunity_id)
);

CREATE INDEX idx_settlement_evidence_result
ON settlement_evidence (result_snapshot_id, settlement_id);

CREATE TABLE settlement_audit_events (
    id TEXT PRIMARY KEY,
    settlement_id TEXT,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    details_json TEXT NOT NULL,
    UNIQUE (settlement_id, event_type, details_json)
);

CREATE TABLE monitoring_runs (
    id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    as_of_utc TEXT NOT NULL,
    window_start_utc TEXT NOT NULL,
    window_end_utc TEXT NOT NULL,
    summary_status TEXT NOT NULL CHECK (
        summary_status IN ('healthy', 'warning', 'critical', 'unknown')
    ),
    report_relative_path TEXT NOT NULL,
    report_checksum_sha256 TEXT NOT NULL,
    evidence_fingerprint TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (
        policy_id, policy_version, as_of_utc,
        window_start_utc, window_end_utc, evidence_fingerprint
    )
);

CREATE TABLE monitoring_findings (
    id TEXT PRIMARY KEY,
    monitoring_run_id TEXT NOT NULL REFERENCES monitoring_runs(id),
    metric_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('healthy', 'warning', 'critical', 'unknown')
    ),
    finding_json TEXT NOT NULL,
    UNIQUE (monitoring_run_id, metric_name)
);

CREATE TABLE model_registry_entries (
    model_artifact_id TEXT PRIMARY KEY,
    model_checksum_sha256 TEXT NOT NULL,
    model_relative_path TEXT NOT NULL,
    model_specification_version TEXT NOT NULL,
    feature_specification_version TEXT NOT NULL,
    sport_code TEXT NOT NULL,
    market_key TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('champion', 'challenger', 'archived')),
    lifecycle_status TEXT NOT NULL CHECK (
        lifecycle_status IN (
            'registered', 'eligible', 'promoted', 'demoted', 'archived', 'rejected'
        )
    ),
    registered_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    superseded_model_artifact_id TEXT REFERENCES model_registry_entries(model_artifact_id),
    version INTEGER NOT NULL DEFAULT 1 CHECK (
        typeof(version) = 'integer' AND version >= 1
    ),
    UNIQUE (model_artifact_id, model_checksum_sha256)
);

CREATE UNIQUE INDEX uq_model_registry_active_champion_scope
ON model_registry_entries (sport_code, market_key)
WHERE role = 'champion' AND lifecycle_status NOT IN ('archived', 'rejected');

CREATE INDEX idx_model_registry_scope_role
ON model_registry_entries (sport_code, market_key, role, model_artifact_id);

CREATE TABLE promotion_decisions (
    id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    champion_model_artifact_id TEXT NOT NULL REFERENCES model_registry_entries(model_artifact_id),
    challenger_model_artifact_id TEXT NOT NULL REFERENCES model_registry_entries(model_artifact_id),
    champion_version INTEGER NOT NULL,
    challenger_version INTEGER NOT NULL,
    evidence_fingerprint TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('promote', 'retain', 'hold', 'reject')),
    as_of_utc TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (
        policy_id, policy_version, champion_model_artifact_id,
        challenger_model_artifact_id, evidence_fingerprint, as_of_utc
    )
);

CREATE TABLE model_role_transitions (
    id TEXT PRIMARY KEY,
    transition_type TEXT NOT NULL CHECK (transition_type IN ('promotion', 'rollback')),
    decision_id TEXT REFERENCES promotion_decisions(id),
    scope_sport_code TEXT NOT NULL,
    scope_market_key TEXT NOT NULL,
    previous_champion_model_artifact_id TEXT NOT NULL REFERENCES model_registry_entries(model_artifact_id),
    new_champion_model_artifact_id TEXT NOT NULL REFERENCES model_registry_entries(model_artifact_id),
    rollback_of_transition_id TEXT REFERENCES model_role_transitions(id),
    actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    details_json TEXT NOT NULL,
    UNIQUE (transition_type, decision_id),
    UNIQUE (rollback_of_transition_id)
);

CREATE INDEX idx_model_role_transitions_scope_time
ON model_role_transitions (
    scope_sport_code, scope_market_key, occurred_at, id
);
