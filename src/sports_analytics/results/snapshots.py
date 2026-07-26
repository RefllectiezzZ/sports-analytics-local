"""Immutable, strictly verified canonical-result snapshot publication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from sports_analytics.artifact_strict import (
    require_canonical_selection_identity,
    require_canonical_utc_timestamp_string,
    require_dict,
    require_int,
    require_list,
    require_sha256_checksum,
    require_str,
    require_str_list,
)
from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, ResultError
from sports_analytics.data.types import JsonValue
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.results.contracts import (
    RESULT_IDENTITY_VERSION,
    RESULT_SCHEMA_VERSION,
    CanonicalResult,
    EventResultStatus,
    MarketOutcome,
    ParticipantResult,
    ResultInputSnapshot,
    build_canonical_result,
)

RESULT_SNAPSHOT_ARTIFACT_TYPE: Final[str] = "canonical-result-snapshot"
RESULT_SNAPSHOT_SCHEMA_VERSION: Final[str] = "canonical-result-snapshot-v1"


@dataclass(frozen=True, slots=True)
class VerifiedResultSnapshot:
    """A canonical result admitted through the immutable artifact trust boundary."""

    snapshot_id: str
    checksum_sha256: str
    relative_directory: str
    result: CanonicalResult


def publish_result_snapshot(
    *,
    root: Path,
    relative_directory: str,
    result: CanonicalResult,
) -> VerifiedResultSnapshot:
    snapshot_id = derive_result_snapshot_id(result)
    payload: dict[str, JsonValue] = {
        "snapshot_id": snapshot_id,
        "snapshot_schema_version": RESULT_SNAPSHOT_SCHEMA_VERSION,
        "result": result.to_json(),
    }
    try:
        artifact = write_analytical_artifact(
            root=root,
            relative_directory=relative_directory,
            artifact_type=RESULT_SNAPSHOT_ARTIFACT_TYPE,
            schema_version=RESULT_SNAPSHOT_SCHEMA_VERSION,
            payload=payload,
        )
    except ArtifactError:
        existing = load_result_snapshot(
            root=root,
            relative_directory=relative_directory,
            expected_snapshot_id=snapshot_id,
        )
        if existing.result != result:
            raise ResultError("existing result snapshot conflicts with publication") from None
        return existing
    return _verified(artifact)


def load_result_snapshot(
    *,
    root: Path,
    relative_directory: str,
    expected_checksum: str | None = None,
    expected_snapshot_id: str | None = None,
) -> VerifiedResultSnapshot:
    try:
        artifact = load_analytical_artifact(
            root=root,
            relative_directory=relative_directory,
            expected_artifact_type=RESULT_SNAPSHOT_ARTIFACT_TYPE,
            expected_schema_version=RESULT_SNAPSHOT_SCHEMA_VERSION,
            expected_checksum=expected_checksum,
        )
        verified = _verified(artifact)
    except ArtifactError as exc:
        raise ResultError(f"result snapshot verification failed: {exc}") from exc
    if expected_snapshot_id is not None and verified.snapshot_id != expected_snapshot_id:
        raise ResultError("result snapshot identity does not match expected snapshot")
    return verified


def derive_result_snapshot_id(result: CanonicalResult) -> str:
    return content_addressed_id(
        identity_type=RESULT_SNAPSHOT_SCHEMA_VERSION,
        payload={
            "canonical_result_id": result.canonical_result_id,
            "source_checksum_sha256": result.source_checksum_sha256,
            "input_snapshots": [item.to_json() for item in result.input_snapshots],
        },
    )


def _verified(artifact: AnalyticalArtifact) -> VerifiedResultSnapshot:
    if not isinstance(artifact.payload, dict) or set(artifact.payload) != {
        "snapshot_id",
        "snapshot_schema_version",
        "result",
    }:
        raise ResultError("result snapshot payload fields are not exact")
    snapshot_id = require_sha256_checksum(
        artifact.payload["snapshot_id"],
        field="snapshot_id",
    )
    if artifact.payload["snapshot_schema_version"] != RESULT_SNAPSHOT_SCHEMA_VERSION:
        raise ResultError("unsupported result snapshot schema version")
    result = _parse_result(require_dict(artifact.payload["result"], field="result"))
    if snapshot_id != derive_result_snapshot_id(result):
        raise ResultError("result snapshot id does not match canonical result evidence")
    return VerifiedResultSnapshot(
        snapshot_id=snapshot_id,
        checksum_sha256=artifact.checksum_sha256,
        relative_directory=artifact.relative_directory,
        result=result,
    )


def _parse_result(row: dict[str, JsonValue]) -> CanonicalResult:
    expected = {
        "canonical_result_id",
        "schema_version",
        "identity_version",
        "canonical_event_id",
        "sport_code",
        "event_status",
        "scheduled_start_utc",
        "result_timestamp_utc",
        "source_name",
        "source_event_id",
        "source_observed_at_utc",
        "result_provenance",
        "participant_results",
        "market_outcomes",
        "input_snapshots",
        "source_checksum_sha256",
        "warnings",
    }
    if set(row) != expected:
        raise ResultError("canonical result fields are not exact")
    if row["schema_version"] != RESULT_SCHEMA_VERSION:
        raise ResultError("canonical result schema version mismatch")
    if row["identity_version"] != RESULT_IDENTITY_VERSION:
        raise ResultError("canonical result identity version mismatch")
    status = require_str(row["event_status"], field="event_status")
    result_timestamp_raw = row["result_timestamp_utc"]
    result_timestamp: datetime | None
    if result_timestamp_raw is None:
        result_timestamp = None
    else:
        result_timestamp = require_canonical_utc_timestamp_string(
            result_timestamp_raw,
            field="result_timestamp_utc",
        )
    participant_results: list[ParticipantResult] = []
    for raw in require_list(row["participant_results"], field="participant_results"):
        item = require_dict(raw, field="participant_results[]")
        if set(item) != {"canonical_participant_id", "role", "score"}:
            raise ResultError("participant result fields are not exact")
        participant_results.append(
            ParticipantResult(
                canonical_participant_id=require_str(
                    item["canonical_participant_id"],
                    field="canonical_participant_id",
                ),
                role=require_str(item["role"], field="role"),
                score=require_int(item["score"], field="score"),
            )
        )
    outcomes: list[MarketOutcome] = []
    for raw in require_list(row["market_outcomes"], field="market_outcomes"):
        item = require_dict(raw, field="market_outcomes[]")
        if set(item) != {"selection_id", "selection", "result"}:
            raise ResultError("market outcome fields are not exact")
        selection = require_canonical_selection_identity(
            item["selection"],
            field="selection",
        )
        if item["selection_id"] != selection.selection_id:
            raise ResultError("market outcome selection id does not match identity")
        outcomes.append(
            MarketOutcome(
                selection=selection,
                result=require_str(item["result"], field="result"),
            )
        )
    inputs: list[ResultInputSnapshot] = []
    for raw in require_list(row["input_snapshots"], field="input_snapshots"):
        item = require_dict(raw, field="input_snapshots[]")
        if set(item) != {"snapshot_id", "checksum_sha256", "schema_version", "source_name"}:
            raise ResultError("input snapshot reference fields are not exact")
        inputs.append(
            ResultInputSnapshot(
                snapshot_id=require_str(item["snapshot_id"], field="snapshot_id"),
                checksum_sha256=require_sha256_checksum(
                    item["checksum_sha256"],
                    field="checksum_sha256",
                ),
                schema_version=require_str(item["schema_version"], field="schema_version"),
                source_name=require_str(item["source_name"], field="source_name"),
            )
        )
    result = build_canonical_result(
        canonical_event_id=require_str(row["canonical_event_id"], field="canonical_event_id"),
        sport_code=require_str(row["sport_code"], field="sport_code"),
        event_status=EventResultStatus(status),
        scheduled_start_utc=require_canonical_utc_timestamp_string(
            row["scheduled_start_utc"],
            field="scheduled_start_utc",
        ),
        result_timestamp_utc=result_timestamp,
        source_name=require_str(row["source_name"], field="source_name"),
        source_event_id=require_str(row["source_event_id"], field="source_event_id"),
        source_observed_at_utc=require_canonical_utc_timestamp_string(
            row["source_observed_at_utc"],
            field="source_observed_at_utc",
        ),
        result_provenance=require_str(row["result_provenance"], field="result_provenance"),
        participant_results=tuple(participant_results),
        market_outcomes=tuple(outcomes),
        input_snapshots=tuple(inputs),
        source_checksum_sha256=require_sha256_checksum(
            row["source_checksum_sha256"],
            field="source_checksum_sha256",
        ),
        warnings=tuple(require_str_list(row["warnings"], field="warnings")),
    )
    if row["canonical_result_id"] != result.canonical_result_id:
        raise ResultError("persisted canonical result identity is forged or stale")
    return result
