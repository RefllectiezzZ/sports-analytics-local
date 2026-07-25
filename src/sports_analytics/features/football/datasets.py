"""Football feature dataset loading, assembly, and immutable artifact I/O."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from sports_analytics.core.exceptions import FeatureError
from sports_analytics.data.codec import (
    dumps_canonical_json,
    ensure_json_value,
    format_utc_timestamp,
    utc_now,
)
from sports_analytics.data.types import JsonValue, validate_sha256_checksum
from sports_analytics.features.contracts import (
    FEATURE_MANIFEST_VERSION,
    FOOTBALL_1X2_FEATURE_NAMES_V1,
    FOOTBALL_1X2_METADATA_COLUMNS,
    FOOTBALL_1X2_PREMATCH_FEATURES_V1,
    SnapshotIdentity,
    football_1x2_prematch_specification,
)
from sports_analytics.features.football.prematch import (
    ELO_CONFIG_VERSION,
    ELO_HOME_ADVANTAGE,
    ELO_INITIAL_RATING,
    ELO_K_FACTOR,
    ELO_SEASON_TRANSITION_POLICY,
    FeatureVector,
    FinishedTrainingEvent,
    generate_prematch_features,
    training_event_from_row,
    validate_training_events,
)
from sports_analytics.ingestion.snapshot_specs import resolve_snapshot_suite
from sports_analytics.snapshots.parquet import file_sha256_and_size, write_parquet_file
from sports_analytics.snapshots.paths import (
    is_absolute_path_text,
    resolve_snapshot_dir,
    resolve_under_root,
)
from sports_analytics.snapshots.reader import verify_snapshot_directory
from sports_analytics.sports.contracts import EventStatus
from sports_analytics.sports.football.markets import (
    MARKET_KEY_MATCH_RESULT_1X2,
    MATCH_RESULT_1X2_OUTCOMES,
)
from sports_analytics.sports.football.schemas import FOOTBALL_CANONICAL_SCHEMA_VERSION
from sports_analytics.sports.identifiers import SPORT_FOOTBALL


@dataclass(frozen=True, slots=True)
class ClosingMarketQuoteTriple:
    """Closing market-average 1X2 decimal odds for one event, when complete."""

    canonical_event_id: str
    home_odds: float
    draw_odds: float
    away_odds: float


@dataclass(frozen=True, slots=True)
class FeatureArtifactPaths:
    """Relative filenames inside a feature artifact directory."""

    features_filename: str = "features.parquet"
    targets_filename: str = "targets.parquet"
    folds_filename: str = "folds.parquet"
    manifest_filename: str = "manifest.json"


@dataclass(frozen=True, slots=True)
class BuiltFeatureArtifact:
    """Result of writing one immutable feature artifact."""

    artifact_id: str
    directory: Path
    relative_directory: str
    manifest_checksum_sha256: str
    feature_row_count: int
    vectors: tuple[FeatureVector, ...]
    snapshot_identities: tuple[SnapshotIdentity, ...]
    closing_quotes: tuple[ClosingMarketQuoteTriple, ...]


def load_finished_events_from_snapshots(
    *,
    snapshots_directory: Path,
    relative_manifest_paths: tuple[str, ...],
    expected_schema_version: str = FOOTBALL_CANONICAL_SCHEMA_VERSION,
) -> tuple[
    tuple[FinishedTrainingEvent, ...],
    tuple[SnapshotIdentity, ...],
    tuple[ClosingMarketQuoteTriple, ...],
]:
    """Load finished canonical events from explicit immutable snapshot manifests.

    Snapshot order does not affect the resulting feature rows: events are merged
    and sorted deterministically. Mixed sports, mixed competitions, incompatible
    schema versions, unresolved/source-scoped training rows, and conflicting
    duplicate canonical events are rejected.
    """
    if not relative_manifest_paths:
        msg = "training inputs must be explicit snapshot manifest paths"
        raise FeatureError(msg)
    # Normalize ordering of input listing for identity recording, but never treat
    # "latest" as implicit: every path must be supplied by the caller.
    identities: list[SnapshotIdentity] = []
    events: list[FinishedTrainingEvent] = []
    quotes: list[ClosingMarketQuoteTriple] = []
    suite = resolve_snapshot_suite(
        snapshot_type="football-ingestion",
        schema_version=expected_schema_version,
    )
    events_fingerprint = suite.schema_fingerprints()["events"]

    for relative_manifest_path in relative_manifest_paths:
        if is_absolute_path_text(relative_manifest_path):
            msg = "snapshot manifest paths must be relative under the snapshots root"
            raise FeatureError(msg)
        verification = verify_snapshot_directory(
            snapshots_directory=snapshots_directory,
            relative_manifest_path=relative_manifest_path,
            suite=suite,
        )
        if verification.schema_version != expected_schema_version:
            msg = (
                "incompatible snapshot schema version: "
                f"expected {expected_schema_version}, got {verification.schema_version}"
            )
            raise FeatureError(msg)
        snapshot_dir = resolve_snapshot_dir(
            snapshots_directory,
            str(Path(relative_manifest_path).parent.as_posix()),
        )
        events_table = _read_parquet(snapshot_dir / "events.parquet")
        quotes_table = _read_parquet(snapshot_dir / "market_quotes.parquet")
        partition_map = dict(verification.partition_keys)
        partition_competition = _require_partition(partition_map, "competition_id")
        season_label = _require_partition(partition_map, "season_label")
        season_id = f"{partition_competition}:{season_label}"
        identities.append(
            SnapshotIdentity(
                snapshot_id=verification.snapshot_id,
                relative_manifest_path=relative_manifest_path.replace("\\", "/"),
                manifest_checksum_sha256=verification.manifest_checksum_sha256,
                schema_version=verification.schema_version,
                schema_fingerprint_events=events_fingerprint,
                competition_id=partition_competition,
                season_id=season_id,
                season_label=season_label,
                sport_code=SPORT_FOOTBALL,
                source_name=verification.source_name,
                event_row_count=verification.row_count("events"),
            )
        )
        for row in events_table.to_pylist():
            if str(row.get("status")) != EventStatus.FINISHED.value:
                continue
            # Canonical events.parquet never contains unresolved/source-scoped rows.
            events.append(training_event_from_row(row))
        quotes.extend(_extract_closing_market_averages(quotes_table))

    competition_ids = {item.competition_id for item in identities}
    if len(competition_ids) != 1:
        msg = f"mixed competitions in snapshot inputs: {sorted(competition_ids)}"
        raise FeatureError(msg)
    fingerprints = {item.schema_fingerprint_events for item in identities}
    if len(fingerprints) != 1:
        msg = "incompatible events schema fingerprints across input snapshots"
        raise FeatureError(msg)

    validated = validate_training_events(tuple(events))
    # Deterministic quote order by event id.
    quote_by_event = {item.canonical_event_id: item for item in quotes}
    ordered_quotes = tuple(quote_by_event[event_id] for event_id in sorted(quote_by_event))
    ordered_identities = tuple(
        sorted(identities, key=lambda item: (item.season_id, item.snapshot_id))
    )
    return validated, ordered_identities, ordered_quotes


def build_feature_artifact(
    *,
    features_root: Path,
    snapshots_directory: Path,
    relative_manifest_paths: tuple[str, ...],
    artifact_id: str | None = None,
    generated_at: datetime | None = None,
    minimum_events: int = 30,
) -> BuiltFeatureArtifact:
    """Build and persist an immutable football 1X2 feature artifact."""
    events, identities, quotes = load_finished_events_from_snapshots(
        snapshots_directory=snapshots_directory,
        relative_manifest_paths=relative_manifest_paths,
    )
    if len(events) < minimum_events:
        msg = (
            "insufficient chronological training history: "
            f"need at least {minimum_events} finished events, found {len(events)}"
        )
        raise FeatureError(msg)

    vectors = generate_prematch_features(events)
    specification = football_1x2_prematch_specification()
    resolved_id = artifact_id or str(uuid4())
    competition_id = identities[0].competition_id
    relative_directory = (
        f"football/{specification.specification_version}/{competition_id}/{resolved_id}"
    ).replace("\\", "/")
    if is_absolute_path_text(relative_directory):
        msg = "feature artifact relative directory must not be absolute"
        raise FeatureError(msg)
    directory = resolve_under_root(
        features_root,
        relative_directory,
        expect_file=False,
        error_type=FeatureError,
    )
    if directory.exists() and any(directory.iterdir()):
        msg = f"feature artifact directory is not empty: {relative_directory}"
        raise FeatureError(msg)
    directory.mkdir(parents=True, exist_ok=True)

    features_table = _features_table(vectors)
    targets_table = _targets_table(vectors)
    folds_table = _empty_folds_table()
    write_parquet_file(directory / "features.parquet", features_table)
    write_parquet_file(directory / "targets.parquet", targets_table)
    write_parquet_file(directory / "folds.parquet", folds_table)

    paths = FeatureArtifactPaths()
    file_meta: dict[str, dict[str, JsonValue]] = {}
    for filename in (
        paths.features_filename,
        paths.targets_filename,
        paths.folds_filename,
    ):
        digest, size = file_sha256_and_size(directory / filename)
        file_meta[filename] = {
            "sha256": digest,
            "byte_count": size,
            "row_count": (
                features_table.num_rows
                if filename == paths.features_filename
                else targets_table.num_rows
                if filename == paths.targets_filename
                else folds_table.num_rows
            ),
        }

    timestamp = generated_at or utc_now()
    manifest: dict[str, JsonValue] = {
        "manifest_version": FEATURE_MANIFEST_VERSION,
        "artifact_id": resolved_id,
        "artifact_type": "football-1x2-feature-dataset",
        "feature_specification_version": specification.specification_version,
        "feature_scope": specification.feature_scope,
        "sport_code": specification.sport_code,
        "market_key": specification.market_key,
        "competition_id": competition_id,
        "season_ids": [item.season_id for item in identities],
        "ordered_feature_names": list(FOOTBALL_1X2_FEATURE_NAMES_V1),
        "metadata_columns": list(FOOTBALL_1X2_METADATA_COLUMNS),
        "elo_configuration": {
            "version": ELO_CONFIG_VERSION,
            "initial_rating": ELO_INITIAL_RATING,
            "k_factor": ELO_K_FACTOR,
            "home_advantage": ELO_HOME_ADVANTAGE,
            "season_transition_policy": ELO_SEASON_TRANSITION_POLICY,
        },
        "leakage_policy": {
            "daily_batching": True,
            "feature_cutoff": "event_date",
            "same_date_isolation": True,
            "odds_as_features": False,
            "post_match_as_features": False,
            "participant_features": False,
        },
        "input_snapshots": [
            {
                "snapshot_id": item.snapshot_id,
                "relative_manifest_path": item.relative_manifest_path,
                "manifest_checksum_sha256": item.manifest_checksum_sha256,
                "schema_version": item.schema_version,
                "schema_fingerprint_events": item.schema_fingerprint_events,
                "competition_id": item.competition_id,
                "season_id": item.season_id,
                "season_label": item.season_label,
                "sport_code": item.sport_code,
                "source_name": item.source_name,
                "event_row_count": item.event_row_count,
            }
            for item in identities
        ],
        "files": ensure_json_value(file_meta),
        "row_counts": {
            "features": features_table.num_rows,
            "targets": targets_table.num_rows,
            "folds": folds_table.num_rows,
            "closing_quote_triples": len(quotes),
        },
        "closing_market_quotes": [
            {
                "canonical_event_id": item.canonical_event_id,
                "home_odds": item.home_odds,
                "draw_odds": item.draw_odds,
                "away_odds": item.away_odds,
            }
            for item in quotes
        ],
        "generated_at_utc": format_utc_timestamp(timestamp),
        "relative_directory": relative_directory,
        "limitations": [
            "Team-level historical football 1X2 baseline features only.",
            "Does not use players, injuries, or lineups.",
            "Does not use bookmaker odds as model features.",
            "Not a betting recommendation engine.",
        ],
    }
    manifest_text = dumps_canonical_json(manifest) + "\n"
    manifest_path = directory / paths.manifest_filename
    manifest_path.write_text(manifest_text, encoding="utf-8", newline="\n")
    manifest_checksum = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
    return BuiltFeatureArtifact(
        artifact_id=resolved_id,
        directory=directory,
        relative_directory=relative_directory,
        manifest_checksum_sha256=manifest_checksum,
        feature_row_count=len(vectors),
        vectors=vectors,
        snapshot_identities=identities,
        closing_quotes=quotes,
    )


def load_feature_artifact(
    *,
    features_root: Path,
    relative_directory: str,
    expected_manifest_checksum: str | None = None,
) -> tuple[dict[str, Any], tuple[FeatureVector, ...], tuple[ClosingMarketQuoteTriple, ...]]:
    """Load and verify a feature artifact from an explicit relative directory."""
    if is_absolute_path_text(relative_directory):
        msg = "feature artifact path must be relative under the features root"
        raise FeatureError(msg)
    directory = resolve_under_root(
        features_root,
        relative_directory.replace("\\", "/"),
        expect_file=False,
        error_type=FeatureError,
    )
    manifest_path = resolve_under_root(
        features_root,
        f"{relative_directory.replace('\\', '/')}/manifest.json",
        expect_file=True,
        error_type=FeatureError,
    )
    raw = manifest_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected_manifest_checksum is not None:
        expected = validate_sha256_checksum(expected_manifest_checksum)
        if digest != expected:
            msg = "feature artifact manifest checksum mismatch"
            raise FeatureError(msg)
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = "feature artifact manifest is malformed"
        raise FeatureError(msg) from exc
    if not isinstance(manifest, dict):
        msg = "feature artifact manifest must be a JSON object"
        raise FeatureError(msg)
    if manifest.get("feature_specification_version") != FOOTBALL_1X2_PREMATCH_FEATURES_V1:
        msg = "unsupported feature specification version in artifact"
        raise FeatureError(msg)
    ordered_names = tuple(manifest.get("ordered_feature_names") or ())
    if ordered_names != FOOTBALL_1X2_FEATURE_NAMES_V1:
        msg = "feature artifact whitelist does not match football-1x2-prematch-features-v1"
        raise FeatureError(msg)

    for filename, meta in (manifest.get("files") or {}).items():
        path = directory / str(filename)
        if not path.is_file():
            msg = f"feature artifact file missing: {filename}"
            raise FeatureError(msg)
        actual, _size = file_sha256_and_size(path)
        expected = str(meta["sha256"])
        if actual != expected:
            msg = f"feature artifact checksum mismatch for {filename}"
            raise FeatureError(msg)

    features_table = _read_parquet(directory / "features.parquet")
    targets_table = _read_parquet(directory / "targets.parquet")
    if set(FOOTBALL_1X2_FEATURE_NAMES_V1).intersection(targets_table.column_names):
        msg = "targets dataset must not contain model feature columns"
        raise FeatureError(msg)
    if "result_code" in features_table.column_names:
        msg = "features dataset must not contain target labels"
        raise FeatureError(msg)

    features_rows = features_table.to_pylist()
    targets_by_id = {
        str(row["canonical_event_id"]): str(row["result_code"]) for row in targets_table.to_pylist()
    }
    if len(features_rows) != len(targets_by_id):
        msg = "features and targets row counts disagree"
        raise FeatureError(msg)

    vectors: list[FeatureVector] = []
    for row in features_rows:
        event_id = str(row["canonical_event_id"])
        result_code = targets_by_id[event_id]
        event_date = _as_date(row["event_date"])
        scheduled = row.get("scheduled_start_utc")
        if hasattr(scheduled, "as_py"):
            scheduled = scheduled.as_py()
        features = {name: float(row[name]) for name in FOOTBALL_1X2_FEATURE_NAMES_V1}
        from sports_analytics.features.contracts import FeatureRowMetadata

        vectors.append(
            FeatureVector(
                metadata=FeatureRowMetadata(
                    canonical_event_id=event_id,
                    competition_id=str(row["competition_id"]),
                    season_id=str(row["season_id"]),
                    event_date=event_date,
                    scheduled_start_utc=scheduled,
                    feature_cutoff_date=_as_date(row["feature_cutoff_date"]),
                    feature_specification_version=str(row["feature_specification_version"]),
                    home_canonical_participant_id=str(row["home_canonical_participant_id"]),
                    away_canonical_participant_id=str(row["away_canonical_participant_id"]),
                ),
                features=features,
                result_code=result_code,
            )
        )
    vectors_sorted = tuple(
        sorted(
            vectors,
            key=lambda item: (
                item.metadata.event_date.isoformat(),
                item.metadata.canonical_event_id,
            ),
        )
    )
    quotes = tuple(
        ClosingMarketQuoteTriple(
            canonical_event_id=str(item["canonical_event_id"]),
            home_odds=float(item["home_odds"]),
            draw_odds=float(item["draw_odds"]),
            away_odds=float(item["away_odds"]),
        )
        for item in (manifest.get("closing_market_quotes") or [])
    )
    return manifest, vectors_sorted, quotes


def write_folds_parquet(
    directory: Path,
    *,
    fold_rows: list[dict[str, object]],
    update_manifest: bool = True,
) -> None:
    """Overwrite folds.parquet inside an existing feature artifact directory."""
    table = pa.Table.from_pylist(fold_rows, schema=_folds_schema())
    write_parquet_file(directory / "folds.parquet", table)
    if not update_manifest:
        return
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        return
    raw = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(raw)
    digest, size = file_sha256_and_size(directory / "folds.parquet")
    files = dict(manifest.get("files") or {})
    files["folds.parquet"] = {
        "sha256": digest,
        "byte_count": size,
        "row_count": table.num_rows,
    }
    manifest["files"] = files
    row_counts = dict(manifest.get("row_counts") or {})
    row_counts["folds"] = table.num_rows
    manifest["row_counts"] = row_counts
    text = dumps_canonical_json(manifest) + "\n"
    manifest_path.write_text(text, encoding="utf-8", newline="\n")


def _features_table(vectors: tuple[FeatureVector, ...]) -> pa.Table:
    rows: list[dict[str, object]] = []
    for item in vectors:
        row: dict[str, object] = {
            "canonical_event_id": item.metadata.canonical_event_id,
            "competition_id": item.metadata.competition_id,
            "season_id": item.metadata.season_id,
            "event_date": item.metadata.event_date,
            "scheduled_start_utc": item.metadata.scheduled_start_utc,
            "feature_cutoff_date": item.metadata.feature_cutoff_date,
            "feature_specification_version": item.metadata.feature_specification_version,
            "home_canonical_participant_id": item.metadata.home_canonical_participant_id,
            "away_canonical_participant_id": item.metadata.away_canonical_participant_id,
        }
        row.update(item.features)
        rows.append(row)
    return pa.Table.from_pylist(rows, schema=_features_schema())


def _targets_table(vectors: tuple[FeatureVector, ...]) -> pa.Table:
    rows = [
        {
            "canonical_event_id": item.metadata.canonical_event_id,
            "event_date": item.metadata.event_date,
            "result_code": item.result_code,
        }
        for item in vectors
    ]
    return pa.Table.from_pylist(rows, schema=_targets_schema())


def _empty_folds_table() -> pa.Table:
    return pa.Table.from_pylist([], schema=_folds_schema())


def _features_schema() -> pa.Schema:
    fields = [
        pa.field("canonical_event_id", pa.string(), nullable=False),
        pa.field("competition_id", pa.string(), nullable=False),
        pa.field("season_id", pa.string(), nullable=False),
        pa.field("event_date", pa.date32(), nullable=False),
        pa.field("scheduled_start_utc", pa.timestamp("us", tz="UTC"), nullable=True),
        pa.field("feature_cutoff_date", pa.date32(), nullable=False),
        pa.field("feature_specification_version", pa.string(), nullable=False),
        pa.field("home_canonical_participant_id", pa.string(), nullable=False),
        pa.field("away_canonical_participant_id", pa.string(), nullable=False),
    ]
    fields.extend(
        pa.field(name, pa.float64(), nullable=False) for name in FOOTBALL_1X2_FEATURE_NAMES_V1
    )
    return pa.schema(fields)


def _targets_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("canonical_event_id", pa.string(), nullable=False),
            pa.field("event_date", pa.date32(), nullable=False),
            pa.field("result_code", pa.string(), nullable=False),
        ]
    )


def _folds_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("fold_id", pa.string(), nullable=False),
            pa.field("region", pa.string(), nullable=False),
            pa.field("canonical_event_id", pa.string(), nullable=False),
            pa.field("event_date", pa.date32(), nullable=False),
        ]
    )


def _read_parquet(path: Path) -> pa.Table:
    if path.is_symlink():
        msg = f"parquet path must not be a symlink: {path.name}"
        raise FeatureError(msg)
    try:
        return pq.read_table(path)
    except Exception as exc:  # noqa: BLE001
        msg = f"failed to read parquet file {path.name}"
        raise FeatureError(msg) from exc


def _extract_closing_market_averages(table: pa.Table) -> list[ClosingMarketQuoteTriple]:
    rows = table.to_pylist()
    grouped: dict[str, dict[str, float]] = {}
    for row in rows:
        if str(row.get("market_key")) != MARKET_KEY_MATCH_RESULT_1X2:
            continue
        if str(row.get("provider_id")) != "market-average":
            continue
        if str(row.get("quote_phase")) != "closing":
            continue
        event_id = str(row["canonical_event_id"])
        outcome = str(row["outcome_key"])
        if outcome not in MATCH_RESULT_1X2_OUTCOMES:
            continue
        odds_value = row["decimal_odds"]
        if hasattr(odds_value, "__float__"):
            odds = float(odds_value)
        else:
            continue
        if odds <= 1.0:
            continue
        grouped.setdefault(event_id, {})[outcome] = odds
    triples: list[ClosingMarketQuoteTriple] = []
    for event_id, outcomes in grouped.items():
        if set(outcomes) != set(MATCH_RESULT_1X2_OUTCOMES):
            continue
        triples.append(
            ClosingMarketQuoteTriple(
                canonical_event_id=event_id,
                home_odds=outcomes["home"],
                draw_odds=outcomes["draw"],
                away_odds=outcomes["away"],
            )
        )
    return triples


def _require_partition(partition_keys: dict[str, str], key: str) -> str:
    value = partition_keys.get(key)
    if value is None or value == "":
        msg = f"snapshot partition key missing: {key}"
        raise FeatureError(msg)
    return str(value)


def _as_date(value: object) -> date:
    if hasattr(value, "as_py"):
        value = value.as_py()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    msg = f"expected date, got {type(value)!r}"
    raise FeatureError(msg)


# Re-export for callers that verify snapshots through feature loading.
__all__ = [
    "BuiltFeatureArtifact",
    "ClosingMarketQuoteTriple",
    "FeatureArtifactPaths",
    "build_feature_artifact",
    "load_feature_artifact",
    "load_finished_events_from_snapshots",
    "write_folds_parquet",
]
