"""Football feature dataset loading, assembly, and immutable artifact I/O."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from sports_analytics.core.exceptions import FeatureError, ModelError
from sports_analytics.data.codec import (
    dumps_canonical_json,
    ensure_json_value,
    format_utc_timestamp,
)
from sports_analytics.data.types import JsonValue, validate_sha256_checksum
from sports_analytics.evaluation.temporal import (
    TemporalFold,
    TemporalSplitConfig,
    assign_fold_rows,
    build_rolling_origin_folds,
    fold_summaries,
)
from sports_analytics.features.contracts import (
    FEATURE_CHECKSUM_SIDECAR,
    FEATURE_MANIFEST_VERSION,
    OutcomeSpace,
    SnapshotIdentity,
)
from sports_analytics.features.football.metadata import FootballFeatureRowMetadata
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
from sports_analytics.features.football.specification import (
    FOOTBALL_1X2_FEATURE_NAMES_V1,
    FOOTBALL_1X2_METADATA_COLUMNS,
    FOOTBALL_1X2_OUTCOME_SPACE,
    FOOTBALL_1X2_PREMATCH_FEATURES_V1,
    football_1x2_prematch_specification,
    football_1x2_target_specification,
)
from sports_analytics.ingestion.snapshot_specs import resolve_snapshot_suite
from sports_analytics.models.identity import content_addressed_id, validate_artifact_id_override
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

FEATURE_PUBLISHED_AT_SIDECAR: str = "published_at.json"
EXPECTED_DATASET_FILES: frozenset[str] = frozenset(
    {"features.parquet", "targets.parquet", "folds.parquet"}
)
VALID_FOLD_REGIONS: frozenset[str] = frozenset({"train", "calibration", "test"})


def _default_feature_split() -> TemporalSplitConfig:
    return TemporalSplitConfig(
        min_train_rows=30,
        min_calibration_rows=10,
        min_test_rows=10,
        step_rows=10,
        maximum_folds=8,
    )


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
    folds: tuple[TemporalFold, ...]
    snapshot_identities: tuple[SnapshotIdentity, ...]
    closing_quotes: tuple[ClosingMarketQuoteTriple, ...]
    split_config: TemporalSplitConfig


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
    """Load finished canonical events from explicit immutable snapshot manifests."""
    if not relative_manifest_paths:
        msg = "training inputs must be explicit snapshot manifest paths"
        raise FeatureError(msg)
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
        identities.append(
            SnapshotIdentity(
                snapshot_id=verification.snapshot_id,
                relative_manifest_path=relative_manifest_path.replace("\\", "/"),
                manifest_checksum_sha256=verification.manifest_checksum_sha256,
                schema_version=verification.schema_version,
                schema_fingerprint_events=events_fingerprint,
                scope_id=partition_competition,
                partition_label=season_label,
                sport_code=SPORT_FOOTBALL,
                source_name=verification.source_name,
                event_row_count=verification.row_count("events"),
            )
        )
        for row in events_table.to_pylist():
            if str(row.get("status")) != EventStatus.FINISHED.value:
                continue
            events.append(training_event_from_row(row))
        quotes.extend(_extract_closing_market_averages(quotes_table))

    competition_ids = {item.scope_id for item in identities}
    if len(competition_ids) != 1:
        msg = f"mixed competitions in snapshot inputs: {sorted(competition_ids)}"
        raise FeatureError(msg)
    fingerprints = {item.schema_fingerprint_events for item in identities}
    if len(fingerprints) != 1:
        msg = "incompatible events schema fingerprints across input snapshots"
        raise FeatureError(msg)

    validated = validate_training_events(tuple(events))
    quote_by_event = {item.canonical_event_id: item for item in quotes}
    ordered_quotes = tuple(quote_by_event[event_id] for event_id in sorted(quote_by_event))
    ordered_identities = tuple(
        sorted(identities, key=lambda item: (item.partition_label, item.snapshot_id))
    )
    return validated, ordered_identities, ordered_quotes


def build_feature_artifact(
    *,
    features_root: Path,
    snapshots_directory: Path,
    relative_manifest_paths: tuple[str, ...],
    split_config: TemporalSplitConfig | None = None,
    artifact_id: str | None = None,
    published_at: datetime | None = None,
    minimum_events: int = 30,
) -> BuiltFeatureArtifact:
    """Build and atomically publish an immutable football 1X2 feature artifact."""
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
    split = split_config or _default_feature_split()
    folds = build_rolling_origin_folds(
        vectors,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        config=split,
    )
    specification = football_1x2_prematch_specification()
    target_spec = football_1x2_target_specification()
    competition_id = identities[0].scope_id
    identity_payload: dict[str, JsonValue] = {
        "input_snapshots": [
            {
                "relative_manifest_path": item.relative_manifest_path,
                "manifest_checksum_sha256": item.manifest_checksum_sha256,
                "snapshot_id": item.snapshot_id,
            }
            for item in identities
        ],
        "feature_specification_version": specification.specification_version,
        "target_specification_version": target_spec.specification_version,
        "ordered_feature_names": list(specification.ordered_feature_names),
        "ordered_outcome_labels": list(target_spec.outcome_space.ordered_labels),
        "fold_configuration": ensure_json_value(split.to_json()),
        "elo_configuration": {
            "version": ELO_CONFIG_VERSION,
            "initial_rating": ELO_INITIAL_RATING,
            "k_factor": ELO_K_FACTOR,
            "home_advantage": ELO_HOME_ADVANTAGE,
            "season_transition_policy": ELO_SEASON_TRANSITION_POLICY,
        },
        "minimum_events": minimum_events,
    }
    derived_id = content_addressed_id(
        identity_type="football-1x2-feature-artifact",
        payload=identity_payload,
    )
    try:
        resolved_id = validate_artifact_id_override(
            override=artifact_id,
            derived=derived_id,
            artifact_kind="feature",
        )
    except ModelError as exc:
        raise FeatureError(str(exc)) from exc
    relative_directory = (
        f"football/{specification.specification_version}/{competition_id}/{resolved_id}"
    ).replace("\\", "/")
    if is_absolute_path_text(relative_directory):
        msg = "feature artifact relative directory must not be absolute"
        raise FeatureError(msg)
    final_directory = resolve_under_root(
        features_root,
        relative_directory,
        expect_file=False,
        error_type=FeatureError,
    )
    if final_directory.exists() and any(final_directory.iterdir()):
        msg = f"feature artifact directory is not empty: {relative_directory}"
        raise FeatureError(msg)

    features_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(
        tempfile.mkdtemp(
            prefix=f".feature-{resolved_id[:8]}-",
            dir=str(features_root.resolve()),
        )
    )
    try:
        features_table = _features_table(vectors)
        targets_table = _targets_table(vectors)
        fold_rows = assign_fold_rows(vectors, folds)
        folds_table = pa.Table.from_pylist(fold_rows, schema=_folds_schema())
        write_parquet_file(temp_dir / "features.parquet", features_table)
        write_parquet_file(temp_dir / "targets.parquet", targets_table)
        write_parquet_file(temp_dir / "folds.parquet", folds_table)

        paths = FeatureArtifactPaths()
        file_meta: dict[str, dict[str, JsonValue]] = {}
        for filename, table in (
            (paths.features_filename, features_table),
            (paths.targets_filename, targets_table),
            (paths.folds_filename, folds_table),
        ):
            digest, size = file_sha256_and_size(temp_dir / filename)
            file_meta[filename] = {
                "sha256": digest,
                "byte_count": size,
                "row_count": table.num_rows,
            }

        manifest: dict[str, JsonValue] = {
            "manifest_version": FEATURE_MANIFEST_VERSION,
            "artifact_id": resolved_id,
            "artifact_type": "football-1x2-feature-dataset",
            "feature_specification_version": specification.specification_version,
            "target_specification_version": target_spec.specification_version,
            "feature_scope": specification.feature_scope,
            "sport_code": specification.sport_code,
            "market_key": specification.market_key,
            "competition_id": competition_id,
            "season_ids": [f"{item.scope_id}:{item.partition_label}" for item in identities],
            "ordered_feature_names": list(FOOTBALL_1X2_FEATURE_NAMES_V1),
            "ordered_outcome_labels": list(FOOTBALL_1X2_OUTCOME_SPACE.ordered_labels),
            "metadata_columns": list(FOOTBALL_1X2_METADATA_COLUMNS),
            "fold_configuration": ensure_json_value(split.to_json()),
            "fold_summaries": ensure_json_value(fold_summaries(folds)),
            "elo_configuration": identity_payload["elo_configuration"],
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
                    "competition_id": item.scope_id,
                    "season_id": f"{item.scope_id}:{item.partition_label}",
                    "season_label": item.partition_label,
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
            "relative_directory": relative_directory,
            "limitations": [
                "Team-level historical football 1X2 baseline features only.",
                "Does not use players, injuries, or lineups.",
                "Does not use bookmaker odds as model features.",
                "Not a betting recommendation engine.",
            ],
        }
        manifest_text = dumps_canonical_json(manifest) + "\n"
        manifest_path = temp_dir / paths.manifest_filename
        manifest_path.write_text(manifest_text, encoding="utf-8", newline="\n")
        manifest_checksum = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
        (temp_dir / FEATURE_CHECKSUM_SIDECAR).write_text(
            f"{manifest_checksum}\n",
            encoding="utf-8",
            newline="\n",
        )
        if published_at is not None:
            published_payload: dict[str, JsonValue] = {
                "published_at_utc": format_utc_timestamp(published_at)
            }
            (temp_dir / FEATURE_PUBLISHED_AT_SIDECAR).write_text(
                dumps_canonical_json(published_payload) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        _verify_feature_directory(temp_dir, manifest=manifest, manifest_checksum=manifest_checksum)
        final_directory.parent.mkdir(parents=True, exist_ok=True)
        temp_dir.rename(final_directory)
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return BuiltFeatureArtifact(
        artifact_id=resolved_id,
        directory=final_directory,
        relative_directory=relative_directory,
        manifest_checksum_sha256=manifest_checksum,
        feature_row_count=len(vectors),
        vectors=vectors,
        folds=folds,
        snapshot_identities=identities,
        closing_quotes=quotes,
        split_config=split,
    )


def load_feature_artifact(
    *,
    features_root: Path,
    relative_directory: str,
    expected_manifest_checksum: str | None = None,
) -> tuple[
    dict[str, Any],
    tuple[FeatureVector, ...],
    tuple[ClosingMarketQuoteTriple, ...],
    tuple[TemporalFold, ...],
]:
    """Load and verify a feature artifact from an explicit relative directory."""
    if is_absolute_path_text(relative_directory):
        msg = "feature artifact path must be relative under the features root"
        raise FeatureError(msg)
    normalized = relative_directory.replace("\\", "/")
    directory = resolve_under_root(
        features_root,
        normalized,
        expect_file=False,
        error_type=FeatureError,
    )
    manifest_path = resolve_under_root(
        features_root,
        f"{normalized}/manifest.json",
        expect_file=True,
        error_type=FeatureError,
    )
    if manifest_path.is_symlink():
        msg = "feature artifact manifest must not be a symlink"
        raise FeatureError(msg)
    sidecar_path = directory / FEATURE_CHECKSUM_SIDECAR
    sidecar_digest = _read_checksum_sidecar(sidecar_path)
    raw = manifest_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != sidecar_digest:
        msg = "feature artifact manifest checksum sidecar mismatch"
        raise FeatureError(msg)
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
    _verify_feature_directory(directory, manifest=manifest, manifest_checksum=digest)

    features_table = _read_parquet(directory / "features.parquet")
    targets_table = _read_parquet(directory / "targets.parquet")
    folds_table = _read_parquet(directory / "folds.parquet")
    if set(FOOTBALL_1X2_FEATURE_NAMES_V1).intersection(targets_table.column_names):
        msg = "targets dataset must not contain model feature columns"
        raise FeatureError(msg)
    if "result_code" in features_table.column_names:
        msg = "features dataset must not contain target labels"
        raise FeatureError(msg)

    features_rows = features_table.to_pylist()
    targets_rows = targets_table.to_pylist()
    allowed_labels = set(FOOTBALL_1X2_OUTCOME_SPACE.ordered_labels)
    targets_by_id: dict[str, str] = {}
    for row in targets_rows:
        event_id = str(row["canonical_event_id"])
        result_code = str(row["result_code"])
        if result_code not in allowed_labels:
            msg = f"invalid target label: {result_code}"
            raise FeatureError(msg)
        targets_by_id[event_id] = result_code
    feature_ids = [str(row["canonical_event_id"]) for row in features_rows]
    target_ids = [str(row["canonical_event_id"]) for row in targets_rows]
    if len(feature_ids) != len(set(feature_ids)):
        msg = "features dataset contains duplicate canonical_event_id values"
        raise FeatureError(msg)
    if sorted(feature_ids) != sorted(target_ids):
        msg = "feature and target canonical_event_id sets must match exactly"
        raise FeatureError(msg)

    vectors: list[FeatureVector] = []
    for row in features_rows:
        event_id = str(row["canonical_event_id"])
        result_code = targets_by_id[event_id]
        scheduled = row.get("scheduled_start_utc")
        if hasattr(scheduled, "as_py"):
            scheduled = scheduled.as_py()
        features = {name: float(row[name]) for name in FOOTBALL_1X2_FEATURE_NAMES_V1}
        vectors.append(
            FeatureVector(
                metadata=FootballFeatureRowMetadata.create(
                    canonical_event_id=event_id,
                    competition_id=str(row["competition_id"]),
                    season_id=str(row["season_id"]),
                    event_date=_as_date(row["event_date"]),
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
    event_date_by_id = {
        item.metadata.canonical_event_id: item.metadata.event_date for item in vectors_sorted
    }
    try:
        fold_config_payload = manifest["fold_configuration"]
        if not isinstance(fold_config_payload, dict):
            raise TypeError
        split_config = TemporalSplitConfig(
            min_train_rows=int(fold_config_payload["min_train_rows"]),
            min_calibration_rows=int(fold_config_payload["min_calibration_rows"]),
            min_test_rows=int(fold_config_payload["min_test_rows"]),
            step_rows=int(fold_config_payload["step_rows"]),
            maximum_folds=int(fold_config_payload["maximum_folds"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        msg = "feature artifact fold_configuration is malformed"
        raise FeatureError(msg) from exc
    folds = reconstruct_folds_from_table(
        folds_table,
        event_date_by_id=event_date_by_id,
        target_label_by_id=targets_by_id,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        split_config=split_config,
        declared_summaries=manifest.get("fold_summaries"),
        latest_feature_date=max(event_date_by_id.values()),
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
    return manifest, vectors_sorted, quotes, folds


def snapshot_feature_artifact_bytes(directory: Path) -> dict[str, bytes]:
    """Return raw bytes for every authoritative file in a feature artifact."""
    files = {
        "manifest.json": (directory / "manifest.json").read_bytes(),
        "manifest_checksum.sha256": (directory / FEATURE_CHECKSUM_SIDECAR).read_bytes(),
        "features.parquet": (directory / "features.parquet").read_bytes(),
        "targets.parquet": (directory / "targets.parquet").read_bytes(),
        "folds.parquet": (directory / "folds.parquet").read_bytes(),
    }
    published = directory / FEATURE_PUBLISHED_AT_SIDECAR
    if published.is_file():
        files[FEATURE_PUBLISHED_AT_SIDECAR] = published.read_bytes()
    return files


def _verify_feature_directory(
    directory: Path,
    *,
    manifest: dict[str, Any],
    manifest_checksum: str,
) -> None:
    if manifest.get("manifest_version") != FEATURE_MANIFEST_VERSION:
        msg = "unsupported feature manifest version"
        raise FeatureError(msg)
    if manifest.get("feature_specification_version") != FOOTBALL_1X2_PREMATCH_FEATURES_V1:
        msg = "unsupported feature specification version in artifact"
        raise FeatureError(msg)
    ordered_names = tuple(manifest.get("ordered_feature_names") or ())
    if ordered_names != FOOTBALL_1X2_FEATURE_NAMES_V1:
        msg = "feature artifact whitelist does not match football-1x2-prematch-features-v1"
        raise FeatureError(msg)
    files = manifest.get("files")
    if not isinstance(files, dict):
        msg = "feature artifact files section is malformed"
        raise FeatureError(msg)
    if set(files) != EXPECTED_DATASET_FILES:
        msg = "feature artifact manifest must describe exactly features, targets, and folds"
        raise FeatureError(msg)
    for filename, meta in files.items():
        if filename != Path(filename).name or filename in {".", ".."}:
            msg = f"unsafe feature artifact filename: {filename}"
            raise FeatureError(msg)
        if ".." in Path(filename).parts:
            msg = f"feature artifact filename must not traverse directories: {filename}"
            raise FeatureError(msg)
        if not isinstance(meta, dict):
            msg = f"feature artifact file metadata is malformed for {filename}"
            raise FeatureError(msg)
        path = resolve_under_root(
            directory,
            filename,
            expect_file=True,
            error_type=FeatureError,
        )
        if path.is_symlink():
            msg = f"feature artifact file must not be a symlink: {filename}"
            raise FeatureError(msg)
        actual, size = file_sha256_and_size(path)
        expected = str(meta["sha256"])
        if actual != expected:
            msg = f"feature artifact checksum mismatch for {filename}"
            raise FeatureError(msg)
        expected_rows = int(meta["row_count"])
        table = _read_parquet(path)
        if table.num_rows != expected_rows:
            msg = f"feature artifact row count mismatch for {filename}"
            raise FeatureError(msg)
        if int(meta["byte_count"]) != size:
            msg = f"feature artifact byte count mismatch for {filename}"
            raise FeatureError(msg)
    row_counts = manifest.get("row_counts") or {}
    if int(row_counts.get("features", -1)) != int(files["features.parquet"]["row_count"]):
        msg = "feature row count mismatch between manifest sections"
        raise FeatureError(msg)
    if int(row_counts.get("targets", -1)) != int(files["targets.parquet"]["row_count"]):
        msg = "target row count mismatch between manifest sections"
        raise FeatureError(msg)
    if int(row_counts.get("folds", -1)) != int(files["folds.parquet"]["row_count"]):
        msg = "fold row count mismatch between manifest sections"
        raise FeatureError(msg)
    if manifest_checksum != hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest():
        msg = "feature artifact manifest checksum verification failed"
        raise FeatureError(msg)
    fold_summaries_payload = manifest.get("fold_summaries")
    if not isinstance(fold_summaries_payload, list) or not fold_summaries_payload:
        msg = "feature artifact fold_summaries are required"
        raise FeatureError(msg)
    features_table = _read_parquet(directory / "features.parquet")
    targets_table = _read_parquet(directory / "targets.parquet")
    folds_table = _read_parquet(directory / "folds.parquet")
    _assert_table_schema(features_table.schema, _features_schema(), "features.parquet")
    _assert_table_schema(targets_table.schema, _targets_schema(), "targets.parquet")
    _assert_table_schema(folds_table.schema, _folds_schema(), "folds.parquet")


def _read_checksum_sidecar(path: Path) -> str:
    if path.is_symlink():
        msg = "checksum sidecar must not be a symlink"
        raise FeatureError(msg)
    if not path.is_file():
        msg = "checksum sidecar is missing"
        raise FeatureError(msg)
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) != 1:
        msg = "checksum sidecar must contain exactly one digest"
        raise FeatureError(msg)
    digest = lines[0].strip()
    validate_sha256_checksum(digest)
    return digest


def _assert_table_schema(actual: pa.Schema, expected: pa.Schema, filename: str) -> None:
    if actual.equals(expected, check_metadata=False):
        return
    msg = f"feature artifact schema mismatch for {filename}"
    raise FeatureError(msg)


def reconstruct_folds_from_table(
    folds_table: pa.Table,
    *,
    event_date_by_id: dict[str, date],
    target_label_by_id: dict[str, str],
    outcome_space: OutcomeSpace,
    split_config: TemporalSplitConfig,
    declared_summaries: object,
    latest_feature_date: date,
) -> tuple[TemporalFold, ...]:
    """Reconstruct and validate temporal folds from persisted fold rows."""
    from sports_analytics.evaluation.temporal import FoldRegion, TemporalFold, fold_summaries

    ordered_labels = outcome_space.ordered_labels
    try:
        rows = folds_table.to_pylist()
    except Exception as exc:  # noqa: BLE001
        msg = "failed to read folds parquet rows"
        raise FeatureError(msg) from exc

    grouped: dict[str, dict[str, list[dict[str, object]]]] = {}
    for row in rows:
        try:
            fold_id = str(row["fold_id"])
            region = str(row["region"])
            event_id = str(row["canonical_event_id"])
            row_date = _as_date(row["event_date"])
        except (KeyError, TypeError, ValueError) as exc:
            msg = "fold row is malformed"
            raise FeatureError(msg) from exc
        if region not in VALID_FOLD_REGIONS:
            msg = f"invalid fold region: {region}"
            raise FeatureError(msg)
        if event_id not in event_date_by_id:
            msg = f"fold references unknown event id: {event_id}"
            raise FeatureError(msg)
        if event_id not in target_label_by_id:
            msg = f"fold references event without target label: {event_id}"
            raise FeatureError(msg)
        if row_date != event_date_by_id[event_id]:
            msg = f"fold event date does not match feature event date for {event_id}"
            raise FeatureError(msg)
        label = target_label_by_id[event_id]
        if label not in ordered_labels:
            msg = f"invalid target label in fold reconstruction: {label}"
            raise FeatureError(msg)
        grouped.setdefault(fold_id, {}).setdefault(region, []).append(row)

    folds: list[TemporalFold] = []
    for fold_id in sorted(grouped):
        regions = grouped[fold_id]
        if set(regions) != VALID_FOLD_REGIONS:
            msg = f"fold {fold_id} is missing one or more required regions"
            raise FeatureError(msg)
        built: dict[str, FoldRegion] = {}
        for region_name in ("train", "calibration", "test"):
            region_rows = sorted(
                regions[region_name],
                key=lambda row: (
                    _as_date(row["event_date"]).isoformat(),
                    str(row["canonical_event_id"]),
                ),
            )
            event_ids = tuple(str(row["canonical_event_id"]) for row in region_rows)
            if len(event_ids) != len(set(event_ids)):
                msg = f"fold {fold_id} region {region_name} contains duplicate events"
                raise FeatureError(msg)
            dates = [event_date_by_id[event_id] for event_id in event_ids]
            if dates != sorted(dates):
                msg = f"fold {fold_id} region {region_name} is not chronological"
                raise FeatureError(msg)
            class_counts = {label: 0 for label in ordered_labels}
            for event_id in event_ids:
                class_counts[target_label_by_id[event_id]] += 1
            built[region_name] = FoldRegion(
                name=region_name,
                start_date=dates[0],
                end_date=dates[-1],
                event_ids=event_ids,
                class_counts=class_counts,
            )
        train_region = built["train"]
        calibration_region = built["calibration"]
        test_region = built["test"]
        train_set = set(train_region.event_ids)
        calibration_set = set(calibration_region.event_ids)
        test_set = set(test_region.event_ids)
        if train_set & calibration_set or train_set & test_set or calibration_set & test_set:
            msg = f"fold {fold_id} assigns an event to multiple regions"
            raise FeatureError(msg)
        date_sets = (
            {event_date_by_id[event_id] for event_id in train_region.event_ids},
            {event_date_by_id[event_id] for event_id in calibration_region.event_ids},
            {event_date_by_id[event_id] for event_id in test_region.event_ids},
        )
        if (
            date_sets[0] & date_sets[1]
            or date_sets[0] & date_sets[2]
            or date_sets[1] & date_sets[2]
        ):
            msg = f"fold {fold_id} regions share calendar dates"
            raise FeatureError(msg)
        if not (
            train_region.end_date < calibration_region.start_date
            and calibration_region.end_date < test_region.start_date
        ):
            msg = f"fold {fold_id} regions overlap or violate chronological ordering"
            raise FeatureError(msg)
        if len(train_region.event_ids) < split_config.min_train_rows:
            msg = f"fold {fold_id} train region below minimum row count"
            raise FeatureError(msg)
        if len(calibration_region.event_ids) < split_config.min_calibration_rows:
            msg = f"fold {fold_id} calibration region below minimum row count"
            raise FeatureError(msg)
        if len(test_region.event_ids) < split_config.min_test_rows:
            msg = f"fold {fold_id} test region below minimum row count"
            raise FeatureError(msg)
        if any(train_region.class_counts[label] < 1 for label in ordered_labels):
            msg = f"fold {fold_id} training region is missing required outcomes"
            raise FeatureError(msg)
        folds.append(
            TemporalFold(
                fold_id=fold_id,
                train=train_region,
                calibration=calibration_region,
                test=test_region,
            )
        )

    if not folds:
        msg = "feature artifact contains no folds"
        raise FeatureError(msg)
    for index in range(1, len(folds)):
        if folds[index - 1].test.end_date > folds[index].test.end_date:
            msg = "persisted folds are not chronologically ordered"
            raise FeatureError(msg)
    if folds[-1].test.end_date != latest_feature_date:
        msg = "final persisted fold does not end on the latest feature date"
        raise FeatureError(msg)

    reconstructed_summaries = fold_summaries(tuple(folds))
    if not isinstance(declared_summaries, list) or not declared_summaries:
        msg = "feature artifact fold_summaries are required"
        raise FeatureError(msg)
    if ensure_json_value(declared_summaries) != ensure_json_value(reconstructed_summaries):
        msg = "persisted fold summaries do not match reconstructed folds"
        raise FeatureError(msg)
    return tuple(folds)


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


__all__ = [
    "BuiltFeatureArtifact",
    "ClosingMarketQuoteTriple",
    "FeatureArtifactPaths",
    "FEATURE_PUBLISHED_AT_SIDECAR",
    "build_feature_artifact",
    "load_feature_artifact",
    "load_finished_events_from_snapshots",
    "snapshot_feature_artifact_bytes",
]
