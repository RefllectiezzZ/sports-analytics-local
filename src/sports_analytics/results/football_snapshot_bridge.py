"""Verified football snapshot to immutable canonical-result bridge."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq

from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, ResultError
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.operations import ResultSnapshotRegistrationRepository
from sports_analytics.data.types import JsonValue
from sports_analytics.ingestion.snapshot_specs import resolve_snapshot_suite
from sports_analytics.results.contracts import (
    EventResultStatus,
    ResultInputSnapshot,
    build_football_full_match_1x2_result,
)
from sports_analytics.results.snapshots import (
    VerifiedResultSnapshot,
    load_result_snapshot,
    publish_result_snapshot,
)
from sports_analytics.snapshots.paths import resolve_snapshot_dir
from sports_analytics.snapshots.reader import SnapshotVerificationResult, verify_snapshot_directory
from sports_analytics.sports.football.schemas import FOOTBALL_CANONICAL_SCHEMA_VERSION

RESULT_BRIDGE_REPORT_TYPE: Final[str] = "football-result-registration-report"
RESULT_BRIDGE_REPORT_SCHEMA: Final[str] = "football-result-registration-report-v1"


@dataclass(frozen=True, slots=True)
class ResultBridgeReport:
    """Deterministic result projection outcome for one verified source snapshot."""

    source_snapshot_id: str
    source_snapshot_checksum_sha256: str
    completed_events: int
    skipped_events: int
    result_snapshots: tuple[VerifiedResultSnapshot, ...]
    report_artifact: AnalyticalArtifact | None = None


def register_completed_results_from_snapshot(
    *,
    database_path: Path,
    snapshots_directory: Path,
    relative_manifest_path: str,
    output_relative_root: str,
    registered_at: datetime,
    actor: str,
) -> ResultBridgeReport:
    """Strictly project and register all completed canonical football events."""
    verification = _verify_source(snapshots_directory, relative_manifest_path)
    source_directory = resolve_snapshot_dir(
        snapshots_directory,
        str(Path(relative_manifest_path).parent.as_posix()),
    )
    rows = pq.read_table(source_directory / "source_events.parquet").to_pylist()
    completed: list[VerifiedResultSnapshot] = []
    skipped = 0
    seen_events: dict[str, tuple[int, int]] = {}
    input_snapshot = ResultInputSnapshot(
        snapshot_id=verification.snapshot_id,
        checksum_sha256=verification.manifest_checksum_sha256,
        schema_version=verification.schema_version,
        source_name=verification.source_name,
    )
    for raw in sorted(rows, key=lambda item: str(item["canonical_event_id"])):
        if raw["status"] != "finished":
            skipped += 1
            continue
        event_id = _text(raw["canonical_event_id"], "canonical_event_id")
        scores = (
            _non_negative_integer(raw["home_score"], "home_score"),
            _non_negative_integer(raw["away_score"], "away_score"),
        )
        previous = seen_events.get(event_id)
        if previous is not None:
            if previous != scores:
                raise ResultError("verified snapshot contains conflicting duplicate final results")
            continue
        seen_events[event_id] = scores
        observed_at = _timestamp(raw["source_observed_at_utc"], "source_observed_at_utc")
        result = build_football_full_match_1x2_result(
            canonical_event_id=event_id,
            scheduled_start_utc=_timestamp(raw["scheduled_start_utc"], "scheduled_start_utc"),
            event_status=EventResultStatus.COMPLETED,
            source_name=_text(raw["source_name"], "source_name"),
            source_event_id=_text(raw["source_event_id"], "source_event_id"),
            source_observed_at_utc=observed_at,
            source_checksum_sha256=_checksum(raw["source_file_sha256"], "source_file_sha256"),
            result_provenance="verified-canonical-football-snapshot",
            home_canonical_participant_id=_text(
                raw["home_canonical_participant_id"],
                "home_canonical_participant_id",
            ),
            away_canonical_participant_id=_text(
                raw["away_canonical_participant_id"],
                "away_canonical_participant_id",
            ),
            full_time_home_score=scores[0],
            full_time_away_score=scores[1],
            result_timestamp_utc=observed_at,
            claimed_outcome_key=_text(raw["result_code"], "result_code"),
            input_snapshots=(input_snapshot,),
        )
        relative_directory = (
            f"{output_relative_root.strip('/')}/{verification.snapshot_id}/{event_id}"
        )
        published = publish_result_snapshot(
            root=snapshots_directory,
            relative_directory=relative_directory,
            result=result,
        )
        reloaded = load_result_snapshot(
            root=snapshots_directory,
            relative_directory=relative_directory,
            expected_checksum=published.checksum_sha256,
            expected_snapshot_id=published.snapshot_id,
        )
        completed.append(reloaded)
    with connect_database(database_path) as connection:
        with transaction(connection, immediate=True):
            repository = ResultSnapshotRegistrationRepository(connection)
            for snapshot in completed:
                existing = connection.execute(
                    """
                    SELECT id, checksum_sha256
                    FROM result_snapshots
                    WHERE canonical_event_id = ? AND event_status = 'completed'
                    """,
                    (snapshot.result.canonical_event_id,),
                ).fetchall()
                if any(
                    str(row["id"]) != snapshot.snapshot_id
                    or str(row["checksum_sha256"]) != snapshot.checksum_sha256
                    for row in existing
                ):
                    raise ResultError("canonical event already has contradictory final evidence")
                repository.register(
                    snapshot=snapshot,
                    registered_at=registered_at,
                    actor=actor,
                )
    report_payload: dict[str, JsonValue] = {
        "source_snapshot_id": verification.snapshot_id,
        "source_snapshot_checksum_sha256": verification.manifest_checksum_sha256,
        "completed_events": len(completed),
        "skipped_events": skipped,
        "result_snapshots": [
            {
                "snapshot_id": item.snapshot_id,
                "checksum_sha256": item.checksum_sha256,
                "canonical_event_id": item.result.canonical_event_id,
                "relative_directory": item.relative_directory,
            }
            for item in completed
        ],
    }
    report_relative_directory = (
        f"{output_relative_root.strip('/')}/{verification.snapshot_id}/registration-report"
    )
    try:
        artifact = write_analytical_artifact(
            root=snapshots_directory,
            relative_directory=report_relative_directory,
            artifact_type=RESULT_BRIDGE_REPORT_TYPE,
            schema_version=RESULT_BRIDGE_REPORT_SCHEMA,
            payload=report_payload,
        )
    except ArtifactError:
        artifact = load_analytical_artifact(
            root=snapshots_directory,
            relative_directory=report_relative_directory,
            expected_artifact_type=RESULT_BRIDGE_REPORT_TYPE,
            expected_schema_version=RESULT_BRIDGE_REPORT_SCHEMA,
        )
        if artifact.payload != report_payload:
            raise ResultError("existing result registration report conflicts") from None
    return ResultBridgeReport(
        source_snapshot_id=verification.snapshot_id,
        source_snapshot_checksum_sha256=verification.manifest_checksum_sha256,
        completed_events=len(completed),
        skipped_events=skipped,
        result_snapshots=tuple(completed),
        report_artifact=artifact,
    )


def _verify_source(
    snapshots_directory: Path,
    relative_manifest_path: str,
) -> SnapshotVerificationResult:
    suite = resolve_snapshot_suite(
        snapshot_type="football-ingestion",
        schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
    )
    verification = verify_snapshot_directory(
        snapshots_directory=snapshots_directory,
        relative_manifest_path=relative_manifest_path,
        suite=suite,
    )
    if verification.source_name != "football-data-co-uk":
        raise ResultError("result bridge accepts only verified Football-Data snapshots")
    return verification


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ResultError(f"{field} must be non-empty canonical text")
    return value


def _checksum(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ResultError(f"{field} must be a lowercase SHA-256 checksum")
    return text


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ResultError(f"{field} must be timezone-aware")
    return value


def _non_negative_integer(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ResultError(f"{field} must be a non-negative integer")
    return value
