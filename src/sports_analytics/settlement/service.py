"""Verified analysis-artifact settlement and immutable report publication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, cast

from sports_analytics.artifact_strict import (
    require_canonical_selection_identity,
    require_decimal_string,
    require_dict,
    require_list,
    require_sha256_checksum,
    require_str,
    require_str_list,
)
from sports_analytics.artifacts import (
    AnalyticalArtifact,
    TypedAnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, SettlementError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.types import JsonValue
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.results.snapshots import VerifiedResultSnapshot
from sports_analytics.settlement.contracts import (
    SETTLEMENT_POLICY_V1,
    SETTLEMENT_VERSION,
    AnalyticalSettlement,
    SettlementPolicy,
    settle_combination,
    settle_single,
)
from sports_analytics.sports.contracts import require_utc

SETTLEMENT_REPORT_TYPE: Final[str] = "analytical-settlement-report"
SETTLEMENT_REPORT_SCHEMA_VERSION: Final[str] = "analytical-settlement-report-v1"


@dataclass(frozen=True, slots=True)
class SettlementReport:
    run_id: str
    source_artifact_id: str
    source_artifact_checksum_sha256: str
    policy_id: str
    policy_version: str
    as_of_utc: datetime
    settlements: tuple[AnalyticalSettlement, ...]
    artifact: AnalyticalArtifact | None = None


def settle_analysis_artifact(
    *,
    artifact: TypedAnalyticalArtifact,
    result_snapshots: tuple[VerifiedResultSnapshot, ...],
    as_of_utc: datetime,
    policy: SettlementPolicy = SETTLEMENT_POLICY_V1,
) -> SettlementReport:
    """Settle every persisted opportunity and combination from verified evidence."""
    if artifact.artifact_kind != "analysis":
        raise SettlementError("operational settlement requires a verified analysis artifact")
    as_of = require_utc(as_of_utc, field_name="as_of_utc")
    results_by_event: dict[str, VerifiedResultSnapshot] = {}
    result_ids: set[str] = set()
    for snapshot in result_snapshots:
        if snapshot.snapshot_id in result_ids:
            raise SettlementError("duplicate result snapshot reference")
        result_ids.add(snapshot.snapshot_id)
        event_id = snapshot.result.canonical_event_id
        existing = results_by_event.get(event_id)
        if existing is not None and existing.snapshot_id != snapshot.snapshot_id:
            raise SettlementError("conflicting result snapshots for one canonical event")
        results_by_event[event_id] = snapshot
    opportunity_rows = {
        cast(str, row["opportunity_id"]): row for row in artifact.dataset("opportunities").rows
    }
    settlements: list[AnalyticalSettlement] = []
    for opportunity_id in sorted(opportunity_rows):
        row = opportunity_rows[opportunity_id]
        event_id = require_str(row["canonical_event_id"], field="canonical_event_id")
        selection = require_canonical_selection_identity(row["selection"], field="selection")
        settlements.append(
            settle_single(
                source_artifact_id=artifact.artifact_id,
                source_artifact_checksum_sha256=artifact.checksum_sha256,
                opportunity_id=opportunity_id,
                canonical_event_id=event_id,
                selection=selection,
                decimal_odds=require_decimal_string(row["decimal_odds"], field="decimal_odds"),
                result_snapshot=results_by_event.get(event_id),
                as_of_utc=as_of,
                policy=policy,
            )
        )
    for row in artifact.dataset("combinations").rows:
        combination_id = require_str(row["combination_id"], field="combination_id")
        opportunity_ids = tuple(require_str_list(row["opportunity_ids"], field="opportunity_ids"))
        legs = []
        for opportunity_id in opportunity_ids:
            opportunity = opportunity_rows.get(opportunity_id)
            if opportunity is None:
                raise SettlementError("combination references a missing persisted opportunity")
            event_id = require_str(
                opportunity["canonical_event_id"],
                field="canonical_event_id",
            )
            legs.append(
                (
                    opportunity_id,
                    event_id,
                    require_canonical_selection_identity(
                        opportunity["selection"],
                        field="selection",
                    ),
                    require_decimal_string(
                        opportunity["decimal_odds"],
                        field="decimal_odds",
                    ),
                    results_by_event.get(event_id),
                )
            )
        settlements.append(
            settle_combination(
                source_artifact_id=artifact.artifact_id,
                source_artifact_checksum_sha256=artifact.checksum_sha256,
                combination_id=combination_id,
                legs=tuple(legs),
                persisted_decimal_odds=require_decimal_string(
                    row["total_decimal_odds"],
                    field="total_decimal_odds",
                ),
                as_of_utc=as_of,
                policy=policy,
            )
        )
    ordered = tuple(sorted(settlements, key=lambda item: item.settlement_id))
    run_id = _run_id(
        artifact_id=artifact.artifact_id,
        artifact_checksum=artifact.checksum_sha256,
        policy=policy,
        as_of_utc=as_of,
        settlements=ordered,
    )
    return SettlementReport(
        run_id=run_id,
        source_artifact_id=artifact.artifact_id,
        source_artifact_checksum_sha256=artifact.checksum_sha256,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        as_of_utc=as_of,
        settlements=ordered,
    )


def publish_settlement_report(
    *,
    root: Path,
    relative_directory: str,
    report: SettlementReport,
) -> SettlementReport:
    payload = _report_payload(report)
    try:
        artifact = write_analytical_artifact(
            root=root,
            relative_directory=relative_directory,
            artifact_type=SETTLEMENT_REPORT_TYPE,
            schema_version=SETTLEMENT_REPORT_SCHEMA_VERSION,
            payload=payload,
        )
    except ArtifactError:
        existing = load_settlement_report(
            root=root,
            relative_directory=relative_directory,
        )
        if existing.artifact is None or existing.artifact.payload != payload:
            raise SettlementError("existing settlement report conflicts with replay") from None
        artifact = existing.artifact
    return SettlementReport(
        run_id=report.run_id,
        source_artifact_id=report.source_artifact_id,
        source_artifact_checksum_sha256=report.source_artifact_checksum_sha256,
        policy_id=report.policy_id,
        policy_version=report.policy_version,
        as_of_utc=report.as_of_utc,
        settlements=report.settlements,
        artifact=artifact,
    )


def load_settlement_report(
    *,
    root: Path,
    relative_directory: str,
    expected_checksum: str | None = None,
) -> SettlementReport:
    try:
        artifact = load_analytical_artifact(
            root=root,
            relative_directory=relative_directory,
            expected_artifact_type=SETTLEMENT_REPORT_TYPE,
            expected_schema_version=SETTLEMENT_REPORT_SCHEMA_VERSION,
            expected_checksum=expected_checksum,
        )
    except ArtifactError as exc:
        raise SettlementError(f"settlement report verification failed: {exc}") from exc
    # The generic artifact verifies exact files, canonical JSON, checksums and content identity.
    # Replaying domain settlement is required for admission; raw report dictionaries are not
    # accepted as settlement inputs. Keep this loader intentionally metadata-only for CLI verify.
    if not isinstance(artifact.payload, dict):
        raise SettlementError("settlement report payload must be an object")
    required = {
        "run_id",
        "schema_version",
        "source_artifact_id",
        "source_artifact_checksum_sha256",
        "policy_id",
        "policy_version",
        "as_of_utc",
        "settlements",
        "disclaimer",
    }
    if set(artifact.payload) != required:
        raise SettlementError("settlement report payload fields are not exact")
    if artifact.payload["schema_version"] != SETTLEMENT_REPORT_SCHEMA_VERSION:
        raise SettlementError("settlement report schema version mismatch")
    _verify_settlement_rows(artifact.payload)
    # Full typed rows remain in the verified payload for inspection.
    return SettlementReport(
        run_id=require_str(artifact.payload["run_id"], field="run_id"),
        source_artifact_id=require_str(
            artifact.payload["source_artifact_id"],
            field="source_artifact_id",
        ),
        source_artifact_checksum_sha256=require_str(
            artifact.payload["source_artifact_checksum_sha256"],
            field="source_artifact_checksum_sha256",
        ),
        policy_id=require_str(artifact.payload["policy_id"], field="policy_id"),
        policy_version=require_str(
            artifact.payload["policy_version"],
            field="policy_version",
        ),
        as_of_utc=datetime.fromisoformat(
            require_str(artifact.payload["as_of_utc"], field="as_of_utc").replace(
                "Z",
                "+00:00",
            )
        ),
        settlements=(),
        artifact=artifact,
    )


def _run_id(
    *,
    artifact_id: str,
    artifact_checksum: str,
    policy: SettlementPolicy,
    as_of_utc: datetime,
    settlements: tuple[AnalyticalSettlement, ...],
) -> str:
    return content_addressed_id(
        identity_type="analytical-settlement-run-v1",
        payload={
            "source_artifact_id": artifact_id,
            "source_artifact_checksum_sha256": artifact_checksum,
            "policy_id": policy.policy_id,
            "policy_version": policy.policy_version,
            "as_of_utc": format_utc_timestamp(as_of_utc),
            "settlement_ids": [item.settlement_id for item in settlements],
        },
    )


def _report_payload(report: SettlementReport) -> dict[str, JsonValue]:
    return {
        "run_id": report.run_id,
        "schema_version": SETTLEMENT_REPORT_SCHEMA_VERSION,
        "source_artifact_id": report.source_artifact_id,
        "source_artifact_checksum_sha256": report.source_artifact_checksum_sha256,
        "policy_id": report.policy_id,
        "policy_version": report.policy_version,
        "as_of_utc": format_utc_timestamp(report.as_of_utc),
        "settlements": [item.to_json() for item in report.settlements],
        "disclaimer": (
            "Analytical flat-unit simulation only; this is not a sportsbook account "
            "or confirmation of bookmaker settlement."
        ),
    }


def _verify_settlement_rows(payload: dict[str, JsonValue]) -> None:
    source_artifact_id = require_str(
        payload["source_artifact_id"],
        field="source_artifact_id",
    )
    source_checksum = require_sha256_checksum(
        payload["source_artifact_checksum_sha256"],
        field="source_artifact_checksum_sha256",
    )
    policy_id = require_str(payload["policy_id"], field="policy_id")
    policy_version = require_str(payload["policy_version"], field="policy_version")
    as_of = require_str(payload["as_of_utc"], field="as_of_utc")
    rows = require_list(payload["settlements"], field="settlements")
    settlement_ids: list[str] = []
    expected_fields = {
        "settlement_id",
        "settlement_version",
        "source_artifact_id",
        "source_artifact_checksum_sha256",
        "position_type",
        "position_id",
        "opportunity_ids",
        "canonical_event_ids",
        "evidence",
        "settlement_as_of_utc",
        "decimal_odds",
        "status",
        "stake_units",
        "returned_units",
        "profit_units",
        "policy_id",
        "policy_version",
        "provenance",
        "warnings",
        "leg_statuses",
    }
    for raw in rows:
        row = require_dict(raw, field="settlements[]")
        if set(row) != expected_fields:
            raise SettlementError("settlement report row fields are not exact")
        if (
            row["settlement_version"] != SETTLEMENT_VERSION
            or row["source_artifact_id"] != source_artifact_id
            or row["source_artifact_checksum_sha256"] != source_checksum
            or row["policy_id"] != policy_id
            or row["policy_version"] != policy_version
            or row["settlement_as_of_utc"] != as_of
        ):
            raise SettlementError("settlement report row lineage is inconsistent")
        decimals = {
            field: require_decimal_string(row[field], field=field)
            for field in ("decimal_odds", "stake_units", "returned_units", "profit_units")
        }
        if (
            decimals["decimal_odds"] <= 1
            or decimals["stake_units"] != 1
            or decimals["returned_units"] - decimals["stake_units"] != decimals["profit_units"]
        ):
            raise SettlementError("settlement report unit calculations are inconsistent")
        status = require_str(row["status"], field="status")
        if status not in {"pending", "win", "loss", "push", "void", "unresolved"}:
            raise SettlementError("settlement report contains an invalid status")
        opportunity_ids = require_str_list(row["opportunity_ids"], field="opportunity_ids")
        if len(opportunity_ids) != len(set(opportunity_ids)):
            raise SettlementError("settlement report row has duplicate opportunities")
        for evidence_raw in require_list(row["evidence"], field="evidence"):
            evidence = require_dict(evidence_raw, field="evidence[]")
            if set(evidence) != {
                "opportunity_id",
                "canonical_event_id",
                "result_snapshot_id",
                "result_checksum_sha256",
                "canonical_result_id",
            }:
                raise SettlementError("settlement evidence fields are not exact")
            require_sha256_checksum(
                evidence["result_snapshot_id"],
                field="result_snapshot_id",
            )
            require_sha256_checksum(
                evidence["result_checksum_sha256"],
                field="result_checksum_sha256",
            )
            require_sha256_checksum(
                evidence["canonical_result_id"],
                field="canonical_result_id",
            )
        identity_payload = {
            key: value
            for key, value in row.items()
            if key not in {"settlement_id", "settlement_version"}
        }
        expected_id = content_addressed_id(
            identity_type=SETTLEMENT_VERSION,
            payload=identity_payload,
        )
        settlement_id = require_sha256_checksum(
            row["settlement_id"],
            field="settlement_id",
        )
        if settlement_id != expected_id:
            raise SettlementError("settlement row identity does not match its evidence")
        settlement_ids.append(settlement_id)
    if settlement_ids != sorted(settlement_ids) or len(settlement_ids) != len(set(settlement_ids)):
        raise SettlementError("settlement report rows are not unique and deterministic")
    expected_run_id = content_addressed_id(
        identity_type="analytical-settlement-run-v1",
        payload={
            "source_artifact_id": source_artifact_id,
            "source_artifact_checksum_sha256": source_checksum,
            "policy_id": policy_id,
            "policy_version": policy_version,
            "as_of_utc": as_of,
            "settlement_ids": cast(list[JsonValue], settlement_ids),
        },
    )
    if payload["run_id"] != expected_run_id:
        raise SettlementError("settlement report run identity is forged or stale")
