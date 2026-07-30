"""Strict real historical snapshot audit with exact immutable lineage."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq

from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError, FeatureError
from sports_analytics.data.types import JsonValue
from sports_analytics.ingestion.snapshot_specs import resolve_snapshot_suite
from sports_analytics.snapshots.paths import resolve_snapshot_dir
from sports_analytics.snapshots.reader import verify_snapshot_directory
from sports_analytics.sports.football.schemas import FOOTBALL_CANONICAL_SCHEMA_VERSION

REAL_DATA_AUDIT_TYPE: Final[str] = "real-football-snapshot-audit"
REAL_DATA_AUDIT_SCHEMA: Final[str] = "real-football-snapshot-audit-v1"


@dataclass(frozen=True, slots=True)
class RealFootballSnapshotAudit:
    snapshot_id: str
    checksum_sha256: str
    source_name: str
    competition_id: str
    season: str
    date_start: str
    date_end: str
    event_count: int
    completed_event_count: int
    missing_result_count: int
    duplicate_identity_count: int
    full_time_score_coverage: float
    half_time_score_coverage: float
    corners_coverage: float
    shots_coverage: float
    shots_on_target_coverage: float
    historical_price_coverage: float
    historical_price_semantics: str
    strict_reload_result: str = "verified"

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "snapshot_id": self.snapshot_id,
            "checksum_sha256": self.checksum_sha256,
            "source_name": self.source_name,
            "competition_id": self.competition_id,
            "season": self.season,
            "date_range": {"start": self.date_start, "end": self.date_end},
            "event_count": self.event_count,
            "completed_event_count": self.completed_event_count,
            "missing_result_count": self.missing_result_count,
            "duplicate_identity_count": self.duplicate_identity_count,
            "coverage": {
                "full_time_scores": self.full_time_score_coverage,
                "half_time_scores": self.half_time_score_coverage,
                "corners": self.corners_coverage,
                "shots": self.shots_coverage,
                "shots_on_target": self.shots_on_target_coverage,
                "historical_prices": self.historical_price_coverage,
            },
            "historical_price_semantics": self.historical_price_semantics,
            "strict_reload_result": self.strict_reload_result,
        }


def audit_verified_real_snapshots(
    *,
    snapshots_directory: Path,
    relative_manifest_paths: tuple[str, ...],
) -> tuple[RealFootballSnapshotAudit, ...]:
    if not relative_manifest_paths:
        raise FeatureError("real snapshot audit requires explicit manifests")
    suite = resolve_snapshot_suite(
        snapshot_type="football-ingestion",
        schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
    )
    audits: list[RealFootballSnapshotAudit] = []
    seen_snapshot_ids: set[str] = set()
    for manifest_path in sorted(relative_manifest_paths):
        verification = verify_snapshot_directory(
            snapshots_directory=snapshots_directory,
            relative_manifest_path=manifest_path,
            suite=suite,
        )
        if verification.snapshot_id in seen_snapshot_ids:
            raise FeatureError("real audit contains duplicate snapshot identity")
        seen_snapshot_ids.add(verification.snapshot_id)
        if verification.source_name != "football-data-co-uk":
            raise FeatureError("real audit accepts only canonical Football-Data snapshots")
        directory = resolve_snapshot_dir(
            snapshots_directory,
            str(Path(manifest_path).parent.as_posix()),
        )
        events = pq.read_table(directory / "events.parquet").to_pylist()
        statistics = pq.read_table(directory / "post_match_statistics.parquet").to_pylist()
        quotes = pq.read_table(directory / "market_quotes.parquet").to_pylist()
        partition = dict(verification.partition_keys)
        event_ids = [str(row["canonical_event_id"]) for row in events]
        completed = [
            row
            for row in events
            if row["status"] == "finished"
            and type(row["home_score"]) is int
            and type(row["away_score"]) is int
            and bool(row["result_code"])
        ]
        stats_by_event = {str(row["canonical_event_id"]): row for row in statistics}
        priced_events = _complete_price_events(quotes)
        dates = sorted(str(row["event_date"]) for row in events)
        denominator = len(completed)
        audits.append(
            RealFootballSnapshotAudit(
                snapshot_id=verification.snapshot_id,
                checksum_sha256=verification.manifest_checksum_sha256,
                source_name=verification.source_name,
                competition_id=partition["competition_id"],
                season=partition["season_label"],
                date_start=dates[0],
                date_end=dates[-1],
                event_count=len(events),
                completed_event_count=denominator,
                missing_result_count=len(events) - denominator,
                duplicate_identity_count=len(event_ids) - len(set(event_ids)),
                full_time_score_coverage=_fraction(denominator, len(events)),
                half_time_score_coverage=_stats_fraction(
                    completed,
                    stats_by_event,
                    ("half_time_home_goals", "half_time_away_goals"),
                ),
                corners_coverage=_stats_fraction(
                    completed,
                    stats_by_event,
                    ("home_corners", "away_corners"),
                ),
                shots_coverage=_stats_fraction(
                    completed,
                    stats_by_event,
                    ("home_shots", "away_shots"),
                ),
                shots_on_target_coverage=_stats_fraction(
                    completed,
                    stats_by_event,
                    ("home_shots_on_target", "away_shots_on_target"),
                ),
                historical_price_coverage=_fraction(
                    len(priced_events.intersection(event_ids)),
                    len(events),
                ),
                historical_price_semantics="historical-closing-benchmark",
            )
        )
    return tuple(sorted(audits, key=lambda item: (item.competition_id, item.season)))


def write_real_data_audit(
    *,
    root: Path,
    relative_directory: str,
    audits: tuple[RealFootballSnapshotAudit, ...],
) -> AnalyticalArtifact:
    return write_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        artifact_type=REAL_DATA_AUDIT_TYPE,
        schema_version=REAL_DATA_AUDIT_SCHEMA,
        payload={
            "snapshot_ids": [item.snapshot_id for item in audits],
            "snapshots": [item.to_json() for item in audits],
            "total_events": sum(item.event_count for item in audits),
            "total_completed_events": sum(item.completed_event_count for item in audits),
            "price_classification": "historical-closing-benchmark",
        },
    )


def load_real_data_audit(
    *,
    root: Path,
    relative_directory: str,
    expected_checksum: str | None = None,
) -> AnalyticalArtifact:
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=REAL_DATA_AUDIT_TYPE,
        expected_schema_version=REAL_DATA_AUDIT_SCHEMA,
        expected_checksum=expected_checksum,
    )
    payload = artifact.payload
    if not isinstance(payload, dict) or set(payload) != {
        "snapshot_ids",
        "snapshots",
        "total_events",
        "total_completed_events",
        "price_classification",
    }:
        raise ArtifactError("real football data audit fields are not exact")
    if payload["price_classification"] != "historical-closing-benchmark":
        raise ArtifactError("real football audit price classification is unsafe")
    snapshots = payload["snapshots"]
    ids = payload["snapshot_ids"]
    if not isinstance(snapshots, list) or not isinstance(ids, list):
        raise ArtifactError("real football audit snapshot lineage is invalid")
    if ids != [item.get("snapshot_id") for item in snapshots if isinstance(item, dict)]:
        raise ArtifactError("real football audit snapshot lineage mismatch")
    return artifact


def _stats_fraction(
    completed: list[dict[str, object]],
    stats_by_event: dict[str, dict[str, object]],
    fields: tuple[str, str],
) -> float:
    count = 0
    for event in completed:
        stats = stats_by_event.get(str(event["canonical_event_id"]))
        if stats is not None and all(type(stats.get(field)) is int for field in fields):
            count += 1
    return _fraction(count, len(completed))


def _complete_price_events(rows: list[dict[str, object]]) -> set[str]:
    outcomes: dict[tuple[str, str, str], set[str]] = {}
    for row in rows:
        if (
            row["market_key"] != "football.match-result.1x2.full-match"
            or row["quote_phase"] != "closing"
            or row["decimal_odds"] is None
        ):
            continue
        key = (
            str(row["canonical_event_id"]),
            str(row["provider_id"]),
            str(row["quote_phase"]),
        )
        outcomes.setdefault(key, set()).add(str(row["outcome_key"]))
    return {
        event_id
        for (event_id, _provider, _phase), values in outcomes.items()
        if values == {"home", "draw", "away"}
    }


def _fraction(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator
