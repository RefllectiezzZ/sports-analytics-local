"""Static snapshot-suite registry and football snapshot specification builder.

The shared snapshot infrastructure never imports a sport package. Instead this
ingestion-owned module resolves the dataset suite for a ``(snapshot_type,
schema_version)`` pair and builds the validated specification the snapshot
service consumes.
"""

from __future__ import annotations

from typing import Final

from sports_analytics.core.exceptions import SnapshotVerificationError
from sports_analytics.data.types import JsonValue
from sports_analytics.snapshots.spec import (
    RawArtifactReference,
    SnapshotDatasetSuite,
    SnapshotHttpMetadata,
    SnapshotIdentity,
    SnapshotSpec,
)
from sports_analytics.sources.catalog import FOOTBALL_DATA_ADAPTER_VERSION
from sports_analytics.sources.football_data_co_uk.adapter import FootballDataAcquisition
from sports_analytics.sources.football_data_co_uk.types import FootballDataCompetition
from sports_analytics.sports.football.contracts import (
    FOOTBALL_CANONICAL_SCHEMA_VERSION,
    FOOTBALL_INGESTION_SNAPSHOT_TYPE,
    FOOTBALL_NORMALIZER_VERSION,
    FOOTBALL_PARSER_VERSION,
    PARTITION_KEY_COMPETITION,
    PARTITION_KEY_SEASON,
)
from sports_analytics.sports.football.identifiers import build_source_version
from sports_analytics.sports.football.normalization import NormalizedFootballBundle
from sports_analytics.sports.football.schemas import football_snapshot_suite
from sports_analytics.sports.identifiers import SPORT_FOOTBALL, build_season_id

_SUITES: Final[dict[tuple[str, str], SnapshotDatasetSuite]] = {
    (
        FOOTBALL_INGESTION_SNAPSHOT_TYPE,
        FOOTBALL_CANONICAL_SCHEMA_VERSION,
    ): football_snapshot_suite(),
}


def resolve_snapshot_suite(*, snapshot_type: str, schema_version: str) -> SnapshotDatasetSuite:
    """Resolve the frozen dataset suite for a snapshot type and schema version."""
    suite = _SUITES.get((snapshot_type, schema_version))
    if suite is None:
        msg = (
            "unsupported snapshot contract: "
            f"snapshot_type={snapshot_type!r} schema_version={schema_version!r}"
        )
        raise SnapshotVerificationError(msg)
    return suite


def supported_snapshot_contracts() -> tuple[tuple[str, str], ...]:
    """Return supported ``(snapshot_type, schema_version)`` pairs in sorted order."""
    return tuple(sorted(_SUITES))


def build_football_snapshot_spec(
    *,
    acquisition: FootballDataAcquisition,
    competition: FootballDataCompetition,
    bundle: NormalizedFootballBundle,
) -> SnapshotSpec:
    """Build the validated snapshot specification for one football ingestion."""
    source_version = build_source_version(
        source_competition_code=competition.division_code,
        source_season_code=acquisition.source_season_code,
        raw_sha256=acquisition.artifact.checksum_sha256,
    )
    identity = SnapshotIdentity(
        snapshot_type=FOOTBALL_INGESTION_SNAPSHOT_TYPE,
        schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
        source_name=acquisition.artifact.source_name,
        source_version=source_version,
        partition_keys=(
            (PARTITION_KEY_COMPETITION, competition.competition_id),
            (PARTITION_KEY_SEASON, acquisition.season_label),
        ),
    )
    http = acquisition.http_metadata
    http_metadata = (
        SnapshotHttpMetadata(
            network_retrieved=True,
            status=http.status_code,
            content_type=http.content_type,
            content_length=http.content_length,
            etag=http.etag,
            last_modified=http.last_modified,
            final_url=http.final_url,
        )
        if http is not None
        else SnapshotHttpMetadata(network_retrieved=False)
    )
    season_id = build_season_id(
        competition_id=competition.competition_id,
        label=acquisition.season_label,
    )
    domain_metadata: dict[str, JsonValue] = {
        "sport_code": SPORT_FOOTBALL,
        "season_id": season_id,
        "source_competition_code": competition.division_code,
        "source_season_code": acquisition.source_season_code,
        "unknown_source_columns": list(acquisition.parsed.unknown_headers),
        "missing_optional_source_columns": list(acquisition.parsed.missing_optional_headers),
    }
    quality_summary = {
        "duplicate_rows_discarded": bundle.duplicate_rows_discarded,
        "warnings_count": len(bundle.warnings),
        "pinnacle_caution_quote_count": bundle.pinnacle_caution_quote_count,
        "unresolved_event_count": bundle.unresolved_event_count,
    }
    return SnapshotSpec(
        identity=identity,
        suite=football_snapshot_suite(),
        source_url=acquisition.source_url,
        source_policy_version=bundle.source_policy_version,
        source_observed_at_utc=acquisition.source_observed_at_utc,
        raw_artifact=RawArtifactReference(
            relative_path=acquisition.artifact.relative_path,
            checksum_sha256=acquisition.artifact.checksum_sha256,
            byte_count=acquisition.artifact.byte_count,
            encoding=acquisition.artifact.encoding,
        ),
        http_metadata=http_metadata,
        producer_versions={
            "adapter": FOOTBALL_DATA_ADAPTER_VERSION,
            "parser": FOOTBALL_PARSER_VERSION,
            "normalizer": FOOTBALL_NORMALIZER_VERSION,
            "event_reconciliation": bundle.reconciliation_policy_version,
            "participant_reconciliation": bundle.participant_reconciliation_policy_version,
        },
        domain_metadata=domain_metadata,
        quality_summary=quality_summary,
        warnings=bundle.warnings,
    )
