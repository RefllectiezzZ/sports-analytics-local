"""Parquet write/read helpers for football-canonical-v1 datasets."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from sports_analytics.core.exceptions import SnapshotIntegrityError
from sports_analytics.snapshots.paths import is_absolute_path_text
from sports_analytics.sports.football.contracts import CANONICAL_DATASETS, PARQUET_FILENAMES
from sports_analytics.sports.football.normalization import (
    CompetitionRecord,
    GameRecord,
    NormalizedFootballBundle,
    OddsQuoteRecord,
    PostMatchStatisticsRecord,
    SeasonRecord,
    TeamRecord,
)
from sports_analytics.sports.football.schemas import (
    ODDS_DECIMAL_PRECISION,
    ODDS_DECIMAL_SCALE,
    dataset_schema,
    schema_fingerprint,
)

PARQUET_COMPRESSION = "zstd"


def _competition_rows(records: tuple[CompetitionRecord, ...]) -> list[dict[str, Any]]:
    return [
        {
            "competition_id": item.competition_id,
            "sport_code": item.sport_code,
            "display_name": item.display_name,
            "country_code": item.country_code,
            "competition_type": item.competition_type,
            "source_name": item.source_name,
            "source_competition_code": item.source_competition_code,
            "timezone": item.timezone,
            "schema_version": item.schema_version,
        }
        for item in records
    ]


def _season_rows(records: tuple[SeasonRecord, ...]) -> list[dict[str, Any]]:
    return [
        {
            "season_id": item.season_id,
            "competition_id": item.competition_id,
            "label": item.label,
            "start_year": item.start_year,
            "end_year": item.end_year,
            "source_season_code": item.source_season_code,
            "schema_version": item.schema_version,
        }
        for item in records
    ]


def _team_rows(records: tuple[TeamRecord, ...]) -> list[dict[str, Any]]:
    return [
        {
            "team_id": item.team_id,
            "sport_code": item.sport_code,
            "source_name": item.source_name,
            "source_team_key": item.source_team_key,
            "display_name": item.display_name,
            "normalized_name": item.normalized_name,
            "schema_version": item.schema_version,
        }
        for item in records
    ]


def _game_rows(records: tuple[GameRecord, ...]) -> list[dict[str, Any]]:
    return [
        {
            "game_id": item.game_id,
            "sport_code": item.sport_code,
            "competition_id": item.competition_id,
            "season_id": item.season_id,
            "source_name": item.source_name,
            "source_game_key": item.source_game_key,
            "source_row_number": item.source_row_number,
            "event_date": item.event_date,
            "scheduled_start_utc": item.scheduled_start_utc,
            "start_time_precision": item.start_time_precision,
            "status": item.status,
            "home_team_id": item.home_team_id,
            "away_team_id": item.away_team_id,
            "full_time_home_goals": item.full_time_home_goals,
            "full_time_away_goals": item.full_time_away_goals,
            "full_time_result": item.full_time_result,
            "half_time_home_goals": item.half_time_home_goals,
            "half_time_away_goals": item.half_time_away_goals,
            "half_time_result": item.half_time_result,
            "source_observed_at_utc": item.source_observed_at_utc,
            "source_file_sha256": item.source_file_sha256,
            "schema_version": item.schema_version,
        }
        for item in records
    ]


def _odds_rows(records: tuple[OddsQuoteRecord, ...]) -> list[dict[str, Any]]:
    return [
        {
            "quote_id": item.quote_id,
            "game_id": item.game_id,
            "market_type": item.market_type,
            "selection": item.selection,
            "provider_type": item.provider_type,
            "provider_id": item.provider_id,
            "quote_phase": item.quote_phase,
            "decimal_odds": item.decimal_odds,
            "source_column": item.source_column,
            "quoted_at_utc": item.quoted_at_utc,
            "source_observed_at_utc": item.source_observed_at_utc,
            "quote_timestamp_precision": item.quote_timestamp_precision,
            "source_file_sha256": item.source_file_sha256,
            "quality_status": item.quality_status,
            "quality_reason": item.quality_reason,
            "schema_version": item.schema_version,
        }
        for item in records
    ]


def _stats_rows(records: tuple[PostMatchStatisticsRecord, ...]) -> list[dict[str, Any]]:
    return [
        {
            "game_id": item.game_id,
            "referee": item.referee,
            "home_shots": item.home_shots,
            "away_shots": item.away_shots,
            "home_shots_on_target": item.home_shots_on_target,
            "away_shots_on_target": item.away_shots_on_target,
            "home_corners": item.home_corners,
            "away_corners": item.away_corners,
            "home_fouls": item.home_fouls,
            "away_fouls": item.away_fouls,
            "home_yellow_cards": item.home_yellow_cards,
            "away_yellow_cards": item.away_yellow_cards,
            "home_red_cards": item.home_red_cards,
            "away_red_cards": item.away_red_cards,
            "availability_stage": item.availability_stage,
            "source_observed_at_utc": item.source_observed_at_utc,
            "source_file_sha256": item.source_file_sha256,
            "schema_version": item.schema_version,
        }
        for item in records
    ]


def bundle_to_tables(bundle: NormalizedFootballBundle) -> dict[str, pa.Table]:
    """Convert a normalized bundle into explicitly typed Arrow tables."""
    mapping = {
        "competitions": (_competition_rows(bundle.competitions), "competitions"),
        "seasons": (_season_rows(bundle.seasons), "seasons"),
        "teams": (_team_rows(bundle.teams), "teams"),
        "games": (_game_rows(bundle.games), "games"),
        "odds_1x2": (_odds_rows(bundle.odds_1x2), "odds_1x2"),
        "post_match_statistics": (
            _stats_rows(bundle.post_match_statistics),
            "post_match_statistics",
        ),
    }
    tables: dict[str, pa.Table] = {}
    for dataset_name, (rows, schema_name) in mapping.items():
        schema = dataset_schema(schema_name)
        if dataset_name == "odds_1x2":
            # Ensure Decimal values match schema scale.
            for row in rows:
                value = row["decimal_odds"]
                if isinstance(value, Decimal):
                    row["decimal_odds"] = value.quantize(Decimal("0.0001"))
        table = pa.Table.from_pylist(rows, schema=schema)
        tables[dataset_name] = table
    return tables


def write_parquet_file(path: Path, table: pa.Table) -> None:
    """Write one Parquet file with deterministic project options."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table,
        where=path,
        compression=PARQUET_COMPRESSION,
        coerce_timestamps="us",
        allow_truncated_timestamps=False,
        use_dictionary=True,
        write_statistics=True,
    )


def verify_parquet_file(path: Path, *, expected_schema: pa.Schema, expected_rows: int) -> None:
    """Re-open a Parquet file and verify schema and row count."""
    if path.is_symlink():
        msg = f"parquet file must not be a symlink: {path.name}"
        raise SnapshotIntegrityError(msg)
    if not path.is_file():
        msg = f"parquet file missing: {path.name}"
        raise SnapshotIntegrityError(msg)
    parquet_file: Any | None = None
    try:
        parquet_file = pq.ParquetFile(path)
        table = parquet_file.read()
        file_metadata = parquet_file.metadata
    except Exception as exc:  # noqa: BLE001 - corrupt files become integrity errors
        msg = f"failed to read parquet file {path.name}"
        raise SnapshotIntegrityError(msg) from exc
    finally:
        if parquet_file is not None:
            close = getattr(parquet_file, "close", None)
            if callable(close):
                close()

    _reject_disallowed_metadata(path.name, table.schema.metadata)
    parquet_metadata = getattr(file_metadata, "metadata", None)
    if isinstance(parquet_metadata, dict):
        _reject_disallowed_metadata(path.name, parquet_metadata)

    if table.num_rows != expected_rows:
        msg = (
            f"parquet row count mismatch for {path.name}: "
            f"expected {expected_rows}, found {table.num_rows}"
        )
        raise SnapshotIntegrityError(msg)
    if schema_fingerprint(table.schema) != schema_fingerprint(expected_schema):
        msg = f"parquet schema mismatch for {path.name}"
        raise SnapshotIntegrityError(msg)


def _reject_disallowed_metadata(
    filename: str,
    metadata: dict[bytes | str, bytes | str] | None,
) -> None:
    if not metadata:
        return
    for key, value in metadata.items():
        decoded_key = key.decode("utf-8", errors="replace") if isinstance(key, bytes) else str(key)
        if "pandas" in decoded_key.lower():
            msg = f"parquet file {filename} must not contain pandas metadata"
            raise SnapshotIntegrityError(msg)
        if decoded_key == "ARROW:schema":
            continue
        decoded_value = (
            value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
        )
        if _contains_absolute_path_metadata(decoded_key) or _contains_absolute_path_metadata(
            decoded_value
        ):
            msg = f"parquet file {filename} must not contain absolute path metadata"
            raise SnapshotIntegrityError(msg)


def _contains_absolute_path_metadata(text: str) -> bool:
    stripped = text.strip()
    if is_absolute_path_text(stripped):
        return True
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return _json_contains_absolute_path(decoded)


def _json_contains_absolute_path(value: object) -> bool:
    if isinstance(value, str):
        return is_absolute_path_text(value.strip())
    if isinstance(value, list):
        return any(_json_contains_absolute_path(item) for item in value)
    if isinstance(value, dict):
        return any(
            _json_contains_absolute_path(key) or _json_contains_absolute_path(item)
            for key, item in value.items()
        )
    return False


def file_sha256_and_size(path: Path) -> tuple[str, int]:
    """Compute SHA-256 and byte size for a regular file."""
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            hasher.update(chunk)
    return hasher.hexdigest(), size


def write_bundle_parquet_files(
    directory: Path,
    bundle: NormalizedFootballBundle,
) -> dict[str, dict[str, object]]:
    """Write and verify all canonical Parquet files into ``directory``.

    Returns per-dataset file metadata used by the manifest writer.
    """
    tables = bundle_to_tables(bundle)
    file_meta: dict[str, dict[str, object]] = {}
    expected_names = {PARQUET_FILENAMES[name] for name in CANONICAL_DATASETS}
    for dataset_name in CANONICAL_DATASETS:
        filename = PARQUET_FILENAMES[dataset_name]
        path = directory / filename
        table = tables[dataset_name]
        write_parquet_file(path, table)
        schema = dataset_schema(dataset_name)
        verify_parquet_file(path, expected_schema=schema, expected_rows=table.num_rows)
        digest, size = file_sha256_and_size(path)
        file_meta[dataset_name] = {
            "relative_filename": filename,
            "sha256": digest,
            "byte_count": size,
            "row_count": table.num_rows,
            "schema_fingerprint": schema_fingerprint(schema),
        }
    # Ensure exactly the expected filenames exist.
    actual = {path.name for path in directory.iterdir() if path.is_file()}
    if actual != expected_names:
        msg = "unexpected files present before manifest write"
        raise SnapshotIntegrityError(msg)
    return file_meta


# Keep date/datetime/Decimal referenced for type checkers / explicit imports.
_ = (date, datetime, ODDS_DECIMAL_PRECISION, ODDS_DECIMAL_SCALE)
