"""Operational repositories for result, settlement, and monitoring evidence."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from sports_analytics.artifacts import AnalyticalArtifact
from sports_analytics.core.exceptions import (
    DatabaseIntegrityError,
    MonitoringError,
    RepositoryError,
    SettlementConflictError,
    SettlementError,
)
from sports_analytics.data.codec import (
    dumps_canonical_json,
    format_utc_timestamp,
    loads_canonical_json,
)
from sports_analytics.data.database import require_active_transaction
from sports_analytics.data.types import JsonValue, validate_identifier
from sports_analytics.monitoring.contracts import MonitoringReport
from sports_analytics.results.snapshots import VerifiedResultSnapshot
from sports_analytics.settlement.service import SettlementReport
from sports_analytics.sports.contracts import require_utc


class ResultSnapshotRegistrationRepository:
    """Idempotent registration of verified immutable result snapshots."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def register(
        self,
        *,
        snapshot: VerifiedResultSnapshot,
        registered_at: datetime,
        actor: str,
    ) -> VerifiedResultSnapshot:
        require_active_transaction(
            self._connection,
            operation="ResultSnapshotRegistrationRepository.register",
        )
        result = snapshot.result
        existing = self._connection.execute(
            "SELECT * FROM result_snapshots WHERE id = ?",
            (snapshot.snapshot_id,),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["checksum_sha256"]) != snapshot.checksum_sha256
                or str(existing["relative_path"]) != snapshot.relative_directory
            ):
                raise RepositoryError("result snapshot identity conflicts with registration")
            return snapshot
        try:
            self._connection.execute(
                """
                INSERT INTO result_snapshots (
                    id, schema_version, identity_version, canonical_event_id,
                    sport_code, event_status, source_name, source_event_id,
                    source_observed_at, result_timestamp, checksum_sha256,
                    relative_path, registered_at, actor, provenance_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    result.schema_version,
                    result.identity_version,
                    result.canonical_event_id,
                    result.sport_code,
                    result.event_status.value,
                    result.source_name,
                    result.source_event_id,
                    format_utc_timestamp(result.source_observed_at_utc),
                    (
                        None
                        if result.result_timestamp_utc is None
                        else format_utc_timestamp(result.result_timestamp_utc)
                    ),
                    snapshot.checksum_sha256,
                    snapshot.relative_directory,
                    format_utc_timestamp(require_utc(registered_at, field_name="registered_at")),
                    validate_identifier(actor, field_name="actor"),
                    dumps_canonical_json(
                        {
                            "canonical_result_id": result.canonical_result_id,
                            "result_provenance": result.result_provenance,
                            "source_checksum_sha256": result.source_checksum_sha256,
                            "input_snapshots": [item.to_json() for item in result.input_snapshots],
                        }
                    ),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DatabaseIntegrityError("result snapshot registration conflicts") from exc
        return snapshot

    def list_registered(self) -> tuple[dict[str, JsonValue], ...]:
        rows = self._connection.execute(
            """
            SELECT * FROM result_snapshots
            ORDER BY source_observed_at, canonical_event_id, id
            """
        ).fetchall()
        return tuple(
            {
                "snapshot_id": str(row["id"]),
                "canonical_event_id": str(row["canonical_event_id"]),
                "event_status": str(row["event_status"]),
                "source_name": str(row["source_name"]),
                "source_event_id": str(row["source_event_id"]),
                "source_observed_at_utc": str(row["source_observed_at"]),
                "checksum_sha256": str(row["checksum_sha256"]),
                "relative_path": str(row["relative_path"]),
            }
            for row in rows
        )


class SettlementRepository:
    """Transactional settlement run/evidence persistence with conflict rejection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def persist_report(
        self,
        *,
        report: SettlementReport,
        actor: str,
        created_at: datetime,
    ) -> str:
        require_active_transaction(
            self._connection,
            operation="SettlementRepository.persist_report",
        )
        if report.artifact is None:
            raise SettlementError(
                "settlement report must be immutably published before persistence"
            )
        timestamp = require_utc(created_at, field_name="created_at")
        existing_run = self._connection.execute(
            "SELECT id, report_checksum_sha256 FROM settlement_runs WHERE id = ?",
            (report.run_id,),
        ).fetchone()
        if existing_run is not None:
            if str(existing_run["report_checksum_sha256"]) != report.artifact.checksum_sha256:
                raise SettlementError("settlement run identity conflicts with stored report")
            return report.run_id
        normalized_actor = validate_identifier(actor, field_name="actor")
        try:
            self._connection.execute(
                """
                INSERT INTO settlement_runs (
                    id, schema_version, source_artifact_id,
                    source_artifact_checksum_sha256, policy_id, policy_version,
                    as_of_utc, report_relative_path, report_checksum_sha256,
                    actor, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.run_id,
                    "analytical-settlement-run-v1",
                    report.source_artifact_id,
                    report.source_artifact_checksum_sha256,
                    report.policy_id,
                    report.policy_version,
                    format_utc_timestamp(report.as_of_utc),
                    report.artifact.relative_directory,
                    report.artifact.checksum_sha256,
                    normalized_actor,
                    format_utc_timestamp(timestamp),
                ),
            )
            for settlement in report.settlements:
                current = self._connection.execute(
                    """
                    SELECT c.settlement_id, c.status, s.evidence_fingerprint
                    FROM current_analytical_settlements c
                    JOIN analytical_settlements s ON s.id = c.settlement_id
                    WHERE c.source_artifact_id = ?
                      AND c.position_type = ?
                      AND c.position_id = ?
                    """,
                    (
                        settlement.source_artifact_id,
                        settlement.position_type,
                        settlement.position_id,
                    ),
                ).fetchone()
                fingerprint = dumps_canonical_json([item.to_json() for item in settlement.evidence])
                if current is not None:
                    if str(current["settlement_id"]) == settlement.settlement_id:
                        continue
                    if str(current["status"]) in {"win", "loss", "push", "void"}:
                        raise SettlementConflictError(
                            "contradictory settlement evidence rejected without overwrite"
                        )
                self._connection.execute(
                    """
                    INSERT INTO analytical_settlements (
                        id, settlement_version, settlement_run_id, position_type,
                        position_id, source_artifact_id,
                        source_artifact_checksum_sha256, status, decimal_odds,
                        stake_units, returned_units, profit_units, policy_id,
                        policy_version, as_of_utc, evidence_fingerprint,
                        record_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        settlement.settlement_id,
                        settlement.settlement_version,
                        report.run_id,
                        settlement.position_type,
                        settlement.position_id,
                        settlement.source_artifact_id,
                        settlement.source_artifact_checksum_sha256,
                        settlement.status.value,
                        format(settlement.decimal_odds, "f"),
                        format(settlement.stake_units, "f"),
                        format(settlement.returned_units, "f"),
                        format(settlement.profit_units, "f"),
                        settlement.policy_id,
                        settlement.policy_version,
                        format_utc_timestamp(settlement.settlement_as_of_utc),
                        fingerprint,
                        dumps_canonical_json(settlement.to_json()),
                        format_utc_timestamp(timestamp),
                    ),
                )
                for evidence in settlement.evidence:
                    self._connection.execute(
                        """
                        INSERT INTO settlement_evidence (
                            settlement_id, opportunity_id, canonical_event_id,
                            result_snapshot_id, result_checksum_sha256
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            settlement.settlement_id,
                            evidence.opportunity_id,
                            evidence.canonical_event_id,
                            evidence.result_snapshot_id,
                            evidence.result_checksum_sha256,
                        ),
                    )
                if current is None:
                    self._connection.execute(
                        """
                        INSERT INTO current_analytical_settlements (
                            source_artifact_id, position_type, position_id,
                            settlement_id, status, updated_at, version
                        ) VALUES (?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            settlement.source_artifact_id,
                            settlement.position_type,
                            settlement.position_id,
                            settlement.settlement_id,
                            settlement.status.value,
                            format_utc_timestamp(timestamp),
                        ),
                    )
                else:
                    self._connection.execute(
                        """
                        UPDATE current_analytical_settlements
                        SET settlement_id = ?, status = ?, updated_at = ?,
                            version = version + 1
                        WHERE source_artifact_id = ?
                          AND position_type = ?
                          AND position_id = ?
                        """,
                        (
                            settlement.settlement_id,
                            settlement.status.value,
                            format_utc_timestamp(timestamp),
                            settlement.source_artifact_id,
                            settlement.position_type,
                            settlement.position_id,
                        ),
                    )
                self._connection.execute(
                    """
                    INSERT INTO settlement_audit_events (
                        id, settlement_id, event_type, actor, occurred_at, details_json
                    ) VALUES (?, ?, 'settlement-recorded', ?, ?, ?)
                    """,
                    (
                        f"recorded:{settlement.settlement_id}",
                        settlement.settlement_id,
                        normalized_actor,
                        format_utc_timestamp(timestamp),
                        dumps_canonical_json({"settlement_run_id": report.run_id}),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DatabaseIntegrityError("settlement persistence violated integrity") from exc
        return report.run_id

    def record_conflicts(
        self,
        *,
        report: SettlementReport,
        actor: str,
        occurred_at: datetime,
    ) -> int:
        """Audit final-position conflicts after the rejected write transaction rolls back."""
        require_active_transaction(
            self._connection,
            operation="SettlementRepository.record_conflicts",
        )
        normalized_actor = validate_identifier(actor, field_name="actor")
        timestamp = require_utc(occurred_at, field_name="occurred_at")
        count = 0
        for settlement in report.settlements:
            current = self._connection.execute(
                """
                SELECT settlement_id, status
                FROM current_analytical_settlements
                WHERE source_artifact_id = ?
                  AND position_type = ?
                  AND position_id = ?
                """,
                (
                    settlement.source_artifact_id,
                    settlement.position_type,
                    settlement.position_id,
                ),
            ).fetchone()
            if (
                current is None
                or str(current["settlement_id"]) == settlement.settlement_id
                or str(current["status"]) not in {"win", "loss", "push", "void"}
            ):
                continue
            self._connection.execute(
                """
                INSERT OR IGNORE INTO settlement_audit_events (
                    id, settlement_id, event_type, actor, occurred_at, details_json
                ) VALUES (?, ?, 'contradictory-evidence-rejected', ?, ?, ?)
                """,
                (
                    f"conflict:{settlement.settlement_id}",
                    str(current["settlement_id"]),
                    normalized_actor,
                    format_utc_timestamp(timestamp),
                    dumps_canonical_json(
                        {
                            "candidate_settlement_id": settlement.settlement_id,
                            "candidate_evidence": [item.to_json() for item in settlement.evidence],
                        }
                    ),
                ),
            )
            count += 1
        return count

    def list_runs(self) -> tuple[dict[str, JsonValue], ...]:
        rows = self._connection.execute(
            """
            SELECT sr.*, COUNT(s.id) AS settlement_count
            FROM settlement_runs sr
            LEFT JOIN analytical_settlements s ON s.settlement_run_id = sr.id
            GROUP BY sr.id
            ORDER BY sr.as_of_utc, sr.id
            """
        ).fetchall()
        return tuple(
            {
                "run_id": str(row["id"]),
                "source_artifact_id": str(row["source_artifact_id"]),
                "policy_id": str(row["policy_id"]),
                "policy_version": str(row["policy_version"]),
                "as_of_utc": str(row["as_of_utc"]),
                "report_relative_path": str(row["report_relative_path"]),
                "report_checksum_sha256": str(row["report_checksum_sha256"]),
                "settlement_count": int(row["settlement_count"]),
            }
            for row in rows
        )


class MonitoringRepository:
    """Idempotent current-index persistence for immutable monitoring reports."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def persist(
        self,
        *,
        report: MonitoringReport,
        artifact: AnalyticalArtifact,
        created_at: datetime,
        actor: str,
    ) -> str:
        require_active_transaction(
            self._connection,
            operation="MonitoringRepository.persist",
        )
        evidence_fingerprint = dumps_canonical_json([item.to_json() for item in report.evidence])
        existing = self._connection.execute(
            "SELECT report_checksum_sha256 FROM monitoring_runs WHERE id = ?",
            (report.run_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["report_checksum_sha256"]) != artifact.checksum_sha256:
                raise MonitoringError("monitoring run conflicts with stored report")
            return report.run_id
        try:
            self._connection.execute(
                """
                INSERT INTO monitoring_runs (
                    id, schema_version, policy_id, policy_version, as_of_utc,
                    window_start_utc, window_end_utc, summary_status,
                    report_relative_path, report_checksum_sha256,
                    evidence_fingerprint, actor, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.run_id,
                    report.schema_version,
                    report.policy_id,
                    report.policy_version,
                    format_utc_timestamp(report.as_of_utc),
                    format_utc_timestamp(report.window_start_utc),
                    format_utc_timestamp(report.window_end_utc),
                    report.summary_status.value,
                    artifact.relative_directory,
                    artifact.checksum_sha256,
                    evidence_fingerprint,
                    validate_identifier(actor, field_name="actor"),
                    format_utc_timestamp(require_utc(created_at, field_name="created_at")),
                ),
            )
            for finding in report.findings:
                self._connection.execute(
                    """
                    INSERT INTO monitoring_findings (
                        id, monitoring_run_id, metric_name, status, finding_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        finding.finding_id,
                        report.run_id,
                        finding.metric_name,
                        finding.status.value,
                        dumps_canonical_json(finding.to_json()),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DatabaseIntegrityError("monitoring persistence violated integrity") from exc
        return report.run_id

    def list_runs(self) -> tuple[dict[str, JsonValue], ...]:
        rows = self._connection.execute(
            "SELECT * FROM monitoring_runs ORDER BY as_of_utc, id"
        ).fetchall()
        return tuple(
            {
                "run_id": str(row["id"]),
                "policy_id": str(row["policy_id"]),
                "policy_version": str(row["policy_version"]),
                "as_of_utc": str(row["as_of_utc"]),
                "summary_status": str(row["summary_status"]),
                "report_relative_path": str(row["report_relative_path"]),
                "report_checksum_sha256": str(row["report_checksum_sha256"]),
                "evidence": loads_canonical_json(str(row["evidence_fingerprint"])),
            }
            for row in rows
        )
