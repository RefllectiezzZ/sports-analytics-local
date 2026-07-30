"""Shared helpers for building synthetic football snapshots in tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa

from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.ingestion.snapshot_specs import build_football_snapshot_spec
from sports_analytics.snapshots.service import SnapshotPublicationService
from sports_analytics.snapshots.spec import SnapshotSpec
from sports_analytics.snapshots.writer import PreparedSnapshot, prepare_snapshot_directory
from sports_analytics.sources.football_data_co_uk.adapter import FootballDataAcquisition
from sports_analytics.sources.football_data_co_uk.catalog import build_csv_url, get_competition
from sports_analytics.sources.football_data_co_uk.parser import parse_football_data_csv
from sports_analytics.sources.football_data_co_uk.types import FootballDataCompetition
from sports_analytics.sources.raw_store import RawSourceArtifact, RawSourceStore
from sports_analytics.sources.types import SOURCE_FOOTBALL_DATA_CO_UK
from sports_analytics.sports.football.normalization import (
    NormalizedFootballBundle,
    normalize_football_rows,
)
from sports_analytics.sports.football.participant_registry import (
    PARTICIPANT_SOURCE_ROLE,
    ParticipantSourceReference,
    derive_participant_registry_artifact,
    load_participant_registry_artifact,
)
from sports_analytics.sports.football.schemas import bundle_to_tables, football_snapshot_suite

OBSERVED_AT = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)

SYNTHETIC_CSV = (
    b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
    b"E0,12/08/2023,Northbridge FC,Southport Athletic,2,1,H\n"
)

SYNTHETIC_CSV_WITH_ODDS = (
    b"Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HTHG,HTAG,HTR,Referee,"
    b"HS,AS,HST,AST,HC,AC,HF,AF,HY,AY,HR,AR,B365H,B365D,B365A\n"
    b"E0,12/08/2023,15:00,Northbridge FC,Southport Athletic,2,1,H,1,0,H,A Smith,"
    b"12,9,5,3,6,4,10,11,1,2,0,0,1.80,3.50,4.20\n"
)


def database_path(tmp_path: Path) -> Path:
    """Create a migrated operational database for tests."""
    path = tmp_path / "operational.sqlite3"
    ensure_database_ready(path)
    return path


def store_artifact(
    raw_directory: Path,
    *,
    content: bytes = SYNTHETIC_CSV,
    source_season_code: str = "2324",
    division_code: str = "E0",
) -> RawSourceArtifact:
    """Store synthetic CSV bytes in a content-addressed raw store."""
    return RawSourceStore(raw_directory).store_bytes(
        source_name=SOURCE_FOOTBALL_DATA_CO_UK,
        source_url=build_csv_url(
            division_code=division_code,
            source_season_code=source_season_code,
        ),
        content=content,
        retrieved_at=OBSERVED_AT,
        content_type="text/csv",
        etag='"etag-1"',
        last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
        encoding="utf-8",
    )


def build_acquisition(
    artifact: RawSourceArtifact,
    *,
    competition: FootballDataCompetition,
    season_label: str = "2023-2024",
    source_season_code: str = "2324",
    content: bytes = SYNTHETIC_CSV,
    network_retrieved: bool = False,
) -> FootballDataAcquisition:
    """Build a synthetic acquisition without any network access."""
    parsed = parse_football_data_csv(content, expected_division_code=competition.division_code)
    start_year = int(season_label.split("-")[0])
    end_year = int(season_label.split("-")[1])
    return FootballDataAcquisition(
        competition_id=competition.competition_id,
        season_label=season_label,
        start_year=start_year,
        end_year=end_year,
        source_season_code=source_season_code,
        source_url=artifact.source_url,
        artifact=artifact,
        parsed=parsed,
        source_observed_at_utc=OBSERVED_AT,
        http_metadata=None,
        network_retrieved=network_retrieved,
    )


def build_bundle(
    artifact: RawSourceArtifact,
    *,
    competition: FootballDataCompetition,
    season_label: str = "2023-2024",
    source_season_code: str = "2324",
    content: bytes = SYNTHETIC_CSV,
    source_name: str | None = None,
) -> NormalizedFootballBundle:
    """Normalize synthetic CSV content into canonical football datasets."""
    parsed = parse_football_data_csv(content, expected_division_code=competition.division_code)
    return normalize_football_rows(
        rows=list(parsed.rows),
        competition_id=competition.competition_id,
        competition_display_name=competition.display_name,
        country_code=competition.country_code,
        source_competition_code=competition.division_code,
        timezone_name=competition.timezone,
        season_label=season_label,
        start_year=int(season_label.split("-")[0]),
        end_year=int(season_label.split("-")[1]),
        source_season_code=source_season_code,
        source_name=source_name or artifact.source_name,
        source_file_sha256=artifact.checksum_sha256,
        source_observed_at_utc=OBSERVED_AT,
    )


def build_spec(
    tmp_path: Path,
    *,
    competition_id: str = "eng-premier-league",
    season_label: str = "2023-2024",
    source_season_code: str = "2324",
    content: bytes = SYNTHETIC_CSV,
    raw_subdirectory: str = "raw",
) -> tuple[SnapshotSpec, NormalizedFootballBundle]:
    """Build a validated football snapshot spec plus its normalized bundle."""
    competition = get_competition(competition_id)
    artifact = store_artifact(
        tmp_path / raw_subdirectory,
        content=content,
        source_season_code=source_season_code,
        division_code=competition.division_code,
    )
    acquisition = build_acquisition(
        artifact,
        competition=competition,
        season_label=season_label,
        source_season_code=source_season_code,
        content=content,
    )
    bundle = build_bundle(
        artifact,
        competition=competition,
        season_label=season_label,
        source_season_code=source_season_code,
        content=content,
    )
    spec = build_football_snapshot_spec(
        acquisition=acquisition,
        competition=competition,
        bundle=bundle,
    )
    return spec, bundle


def build_tables(bundle: NormalizedFootballBundle) -> dict[str, pa.Table]:
    """Convert a bundle into Arrow tables for the football suite."""
    return bundle_to_tables(bundle)


def prepare(
    tmp_path: Path,
    *,
    snapshot_id: str,
    snapshots_directory: Path | None = None,
    competition_id: str = "eng-premier-league",
    season_label: str = "2023-2024",
    source_season_code: str = "2324",
    content: bytes = SYNTHETIC_CSV,
) -> PreparedSnapshot:
    """Prepare a synthetic football snapshot directory."""
    root = snapshots_directory if snapshots_directory is not None else tmp_path / "snapshots"
    spec, bundle = build_spec(
        tmp_path,
        competition_id=competition_id,
        season_label=season_label,
        source_season_code=source_season_code,
        content=content,
        raw_subdirectory=f"raw-{snapshot_id}",
    )
    return prepare_snapshot_directory(
        snapshots_directory=root,
        spec=spec,
        tables=build_tables(bundle),
        snapshot_id=snapshot_id,
    )


def publication_service(
    database: Path,
    snapshots_directory: Path,
) -> SnapshotPublicationService:
    """Build a publication service bound to the football dataset suite."""
    return SnapshotPublicationService(
        database_path=database,
        snapshots_directory=snapshots_directory,
        suite=football_snapshot_suite(),
    )


def build_verified_participant_registry(
    tmp_path: Path,
    *,
    root: Path,
    canonical_participant_ids: tuple[str, str],
    relative_directory: str,
    competition_id: str = "prt-primeira-liga",
    evaluated_at_utc: datetime,
):
    """Build a registry fixture through the real canonical snapshot trust boundary."""
    division = get_competition(competition_id).division_code
    first, second = sorted(canonical_participant_ids)
    content = (
        "Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
        f"{division},12/08/2023,Team {first[:8]},Team {second[:8]},2,1,H\n"
    ).encode()
    spec, bundle = build_spec(
        tmp_path,
        competition_id=competition_id,
        content=content,
        raw_subdirectory=f"raw-{first[:8]}-{second[:8]}",
    )
    tables = build_tables(bundle)
    canonical_rows = tables["participants"].to_pylist()
    old_ids = sorted(str(row["canonical_participant_id"]) for row in canonical_rows)
    replacements = dict(zip(old_ids, (first, second), strict=True))
    for dataset in ("participants", "source_participants", "participant_reconciliations"):
        rows = tables[dataset].to_pylist()
        for row in rows:
            current = row.get("canonical_participant_id")
            if current is not None:
                row["canonical_participant_id"] = replacements[str(current)]
        tables[dataset] = pa.Table.from_pylist(rows, schema=tables[dataset].schema)
    event_rows = tables["events"].to_pylist()
    for row in event_rows:
        row["home_canonical_participant_id"] = replacements[
            str(row["home_canonical_participant_id"])
        ]
        row["away_canonical_participant_id"] = replacements[
            str(row["away_canonical_participant_id"])
        ]
    tables["events"] = pa.Table.from_pylist(event_rows, schema=tables["events"].schema)
    snapshot_id = str(uuid.uuid5(uuid.NAMESPACE_URL, relative_directory))
    prepared = prepare_snapshot_directory(
        snapshots_directory=root,
        spec=spec,
        tables=tables,
        snapshot_id=snapshot_id,
    )
    database = database_path(tmp_path)
    published = publication_service(database, root).publish_or_reuse(
        prepared,
        actor="test",
    )
    source_relative = str(Path(published.snapshot_relative_path).parent).replace("\\", "/")
    reference = ParticipantSourceReference(
        PARTICIPANT_SOURCE_ROLE,
        source_relative,
        published.snapshot_id,
        published.manifest_checksum_sha256,
        published.snapshot_type,
        published.schema_version,
    )
    artifact = derive_participant_registry_artifact(
        root=root,
        source_root=root,
        relative_directory=relative_directory,
        registry_revision="registry-1",
        evaluated_at_utc=evaluated_at_utc,
        source_artifacts=(reference,),
    )
    registry = load_participant_registry_artifact(
        root=root,
        source_root=root,
        relative_directory=relative_directory,
        expected_artifact_id=artifact.artifact_id,
        expected_checksum=artifact.checksum_sha256,
    )
    return artifact, registry, reference
