"""Strict content-addressed analytical JSON artifact publication."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, cast

from sports_analytics.core.exceptions import ArtifactError, RepositoryError
from sports_analytics.data.codec import dumps_canonical_json, ensure_json_value
from sports_analytics.data.types import JsonValue, validate_sha256_checksum
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.snapshots.paths import is_absolute_path_text, resolve_under_root

ANALYTICAL_ARTIFACT_MANIFEST_VERSION: Final[str] = "analytical-artifact-manifest-v1"
ANALYTICAL_MANIFEST_FILENAME: Final[str] = "manifest.json"
ANALYTICAL_CHECKSUM_FILENAME: Final[str] = "manifest_checksum.sha256"
ANALYTICAL_ARTIFACT_FILES: Final[frozenset[str]] = frozenset(
    {ANALYTICAL_MANIFEST_FILENAME, ANALYTICAL_CHECKSUM_FILENAME}
)
TYPED_ARTIFACT_MANIFEST_VERSION: Final[str] = "typed-analytical-artifact-v1"
TYPED_DATASET_LAYOUTS: Final[dict[str, tuple[str, ...]]] = {
    "analysis": (
        "predictions",
        "market_evaluations",
        "opportunity_decisions",
        "opportunities",
        "combinations",
        "rejections",
    ),
    "backtest": (
        "predictions",
        "market_evaluations",
        "opportunity_decisions",
        "opportunities",
        "combinations",
        "rejections",
        "settlements",
        "fold_metrics",
        "aggregate_metrics",
    ),
}


@dataclass(frozen=True, slots=True)
class AnalyticalArtifact:
    """Verified analytical artifact document and filesystem identity."""

    relative_directory: str
    artifact_id: str
    artifact_type: str
    schema_version: str
    checksum_sha256: str
    payload: JsonValue


@dataclass(frozen=True, slots=True)
class TypedDataset:
    """One verified authoritative canonical-JSONL logical dataset."""

    name: str
    filename: str
    schema_version: str
    id_field: str
    row_count: int
    checksum_sha256: str
    rows: tuple[dict[str, JsonValue], ...]


@dataclass(frozen=True, slots=True)
class TypedAnalyticalArtifact:
    """Verified multi-dataset analysis or backtest artifact."""

    relative_directory: str
    artifact_id: str
    artifact_kind: str
    schema_version: str
    checksum_sha256: str
    datasets: tuple[TypedDataset, ...]

    def dataset(self, name: str) -> TypedDataset:
        for dataset in self.datasets:
            if dataset.name == name:
                return dataset
        raise ArtifactError(f"typed artifact dataset is absent: {name}")


def build_analytical_artifact_document(
    *,
    artifact_type: str,
    schema_version: str,
    payload: JsonValue,
) -> dict[str, JsonValue]:
    """Build the exact envelope and content-addressed artifact identity."""
    if not artifact_type or not schema_version:
        raise ArtifactError("artifact_type and schema_version must be non-empty")
    try:
        canonical_payload = ensure_json_value(payload)
    except RepositoryError as exc:
        raise ArtifactError("analytical artifact payload is not canonical JSON") from exc
    artifact_id = content_addressed_id(
        identity_type=f"analytical-artifact:{artifact_type}:{schema_version}",
        payload={"payload": canonical_payload},
    )
    return {
        "manifest_version": ANALYTICAL_ARTIFACT_MANIFEST_VERSION,
        "artifact_type": artifact_type,
        "schema_version": schema_version,
        "artifact_id": artifact_id,
        "payload": canonical_payload,
    }


def write_analytical_artifact(
    *,
    root: Path,
    relative_directory: str,
    artifact_type: str,
    schema_version: str,
    payload: JsonValue,
) -> AnalyticalArtifact:
    """Atomically publish a new immutable two-file artifact directory."""
    document = build_analytical_artifact_document(
        artifact_type=artifact_type,
        schema_version=schema_version,
        payload=payload,
    )
    if is_absolute_path_text(relative_directory):
        raise ArtifactError("analytical artifact path must be relative")
    root.mkdir(parents=True, exist_ok=True)
    final_directory = _resolve(
        root,
        relative_directory,
        expect_file=False,
    )
    if final_directory.exists():
        raise ArtifactError("analytical artifact directory already exists")
    temp_dir = Path(
        tempfile.mkdtemp(
            prefix=f".artifact-{str(document['artifact_id'])[:8]}-",
            dir=str(root.resolve()),
        )
    )
    try:
        text = dumps_canonical_json(document) + "\n"
        (temp_dir / ANALYTICAL_MANIFEST_FILENAME).write_text(
            text,
            encoding="utf-8",
            newline="\n",
        )
        checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
        (temp_dir / ANALYTICAL_CHECKSUM_FILENAME).write_text(
            f"{checksum}\n",
            encoding="utf-8",
            newline="\n",
        )
        _verify_directory(
            temp_dir,
            expected_artifact_type=artifact_type,
            expected_schema_version=schema_version,
            expected_checksum=checksum,
        )
        final_directory.parent.mkdir(parents=True, exist_ok=True)
        temp_dir.rename(final_directory)
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return load_analytical_artifact(
        root=root,
        relative_directory=relative_directory,
        expected_artifact_type=artifact_type,
        expected_schema_version=schema_version,
        expected_checksum=checksum,
    )


def load_analytical_artifact(
    *,
    root: Path,
    relative_directory: str,
    expected_artifact_type: str,
    expected_schema_version: str,
    expected_checksum: str | None = None,
    expected_artifact_id: str | None = None,
) -> AnalyticalArtifact:
    """Strictly verify checksums, schema, content identity, paths, and exact files."""
    directory = _resolve(root, relative_directory, expect_file=False)
    return _verify_directory(
        directory,
        expected_artifact_type=expected_artifact_type,
        expected_schema_version=expected_schema_version,
        expected_checksum=expected_checksum,
        expected_artifact_id=expected_artifact_id,
        relative_directory=relative_directory,
    )


_TYPED_ID_FIELDS: Final[dict[str, str]] = {
    "predictions": "prediction_id",
    "market_evaluations": "evaluation_id",
    "opportunity_decisions": "opportunity_id",
    "opportunities": "opportunity_id",
    "combinations": "combination_id",
    "rejections": "rejection_id",
    "settlements": "bet_id",
    "fold_metrics": "fold_id",
    "aggregate_metrics": "metric_id",
}


def write_typed_analytical_artifact(
    *,
    root: Path,
    relative_directory: str,
    artifact_kind: str,
    schema_version: str,
    datasets: Mapping[str, tuple[dict[str, JsonValue], ...]],
    dataset_schema_versions: Mapping[str, str] | None = None,
) -> TypedAnalyticalArtifact:
    """Atomically publish exact, typed authoritative JSONL datasets."""
    layout = _typed_layout(artifact_kind)
    if set(datasets) != set(layout):
        raise ArtifactError("typed artifact datasets do not exactly match its layout")
    if type(schema_version) is not str or not schema_version:
        raise ArtifactError("typed artifact schema_version must be non-empty")
    schemas = dataset_schema_versions or {}
    normalized: dict[str, tuple[dict[str, JsonValue], ...]] = {}
    dataset_entries: list[dict[str, JsonValue]] = []
    file_bytes: dict[str, bytes] = {}
    for name in layout:
        rows = _canonical_dataset_rows(
            name,
            datasets[name],
            id_field=_TYPED_ID_FIELDS[name],
        )
        normalized[name] = rows
        filename = f"{name}.jsonl"
        raw = "".join(f"{dumps_canonical_json(row)}\n" for row in rows).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        file_bytes[filename] = raw
        dataset_entries.append(
            {
                "name": name,
                "filename": filename,
                "schema_version": schemas.get(name, f"{name}-v1"),
                "id_field": _TYPED_ID_FIELDS[name],
                "row_count": len(rows),
                "sha256": digest,
            }
        )
    identity_payload: dict[str, JsonValue] = {
        "artifact_kind": artifact_kind,
        "schema_version": schema_version,
        "datasets": cast(list[JsonValue], dataset_entries),
    }
    artifact_id = content_addressed_id(
        identity_type=TYPED_ARTIFACT_MANIFEST_VERSION,
        payload=identity_payload,
    )
    manifest: dict[str, JsonValue] = {
        "manifest_version": TYPED_ARTIFACT_MANIFEST_VERSION,
        "artifact_id": artifact_id,
        **identity_payload,
    }
    normalized_relative = relative_directory.replace("\\", "/")
    if is_absolute_path_text(normalized_relative):
        raise ArtifactError("typed artifact path must be relative")
    root.mkdir(parents=True, exist_ok=True)
    final_directory = _resolve(root, normalized_relative, expect_file=False)
    if final_directory.exists():
        raise ArtifactError("typed artifact directory already exists")
    temp_dir = Path(
        tempfile.mkdtemp(
            prefix=f".typed-{artifact_id[:8]}-",
            dir=str(root.resolve()),
        )
    )
    try:
        for filename, raw in file_bytes.items():
            (temp_dir / filename).write_bytes(raw)
        manifest_raw = (dumps_canonical_json(manifest) + "\n").encode("utf-8")
        (temp_dir / ANALYTICAL_MANIFEST_FILENAME).write_bytes(manifest_raw)
        checksum = hashlib.sha256(manifest_raw).hexdigest()
        (temp_dir / ANALYTICAL_CHECKSUM_FILENAME).write_text(
            f"{checksum}\n",
            encoding="utf-8",
            newline="\n",
        )
        _verify_typed_directory(
            temp_dir,
            expected_kind=artifact_kind,
            expected_schema_version=schema_version,
            expected_checksum=checksum,
        )
        final_directory.parent.mkdir(parents=True, exist_ok=True)
        temp_dir.rename(final_directory)
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return load_typed_analytical_artifact(
        root=root,
        relative_directory=normalized_relative,
        expected_kind=artifact_kind,
        expected_schema_version=schema_version,
        expected_checksum=checksum,
    )


def load_typed_analytical_artifact(
    *,
    root: Path,
    relative_directory: str,
    expected_kind: str,
    expected_schema_version: str,
    expected_checksum: str | None = None,
    expected_artifact_id: str | None = None,
) -> TypedAnalyticalArtifact:
    """Strictly verify a typed artifact, every dataset, and semantic invariants."""
    normalized = relative_directory.replace("\\", "/")
    directory = _resolve(root, normalized, expect_file=False)
    return _verify_typed_directory(
        directory,
        expected_kind=expected_kind,
        expected_schema_version=expected_schema_version,
        expected_checksum=expected_checksum,
        expected_artifact_id=expected_artifact_id,
        relative_directory=normalized,
    )


def _verify_typed_directory(
    directory: Path,
    *,
    expected_kind: str,
    expected_schema_version: str,
    expected_checksum: str | None,
    expected_artifact_id: str | None = None,
    relative_directory: str = ".",
) -> TypedAnalyticalArtifact:
    layout = _typed_layout(expected_kind)
    expected_files = {
        ANALYTICAL_MANIFEST_FILENAME,
        ANALYTICAL_CHECKSUM_FILENAME,
        *(f"{name}.jsonl" for name in layout),
    }
    if directory.is_symlink() or not directory.is_dir():
        raise ArtifactError("typed artifact path must be a real directory")
    entries = tuple(directory.iterdir())
    if {item.name for item in entries} != expected_files:
        raise ArtifactError("typed artifact directory has missing or extra files")
    if any(item.is_symlink() or not item.is_file() for item in entries):
        raise ArtifactError("typed artifact files must be regular files, not symlinks")
    sidecar = _one_checksum(directory / ANALYTICAL_CHECKSUM_FILENAME)
    manifest_raw = (directory / ANALYTICAL_MANIFEST_FILENAME).read_bytes()
    manifest_checksum = hashlib.sha256(manifest_raw).hexdigest()
    if sidecar != manifest_checksum:
        raise ArtifactError("typed artifact manifest checksum sidecar mismatch")
    if expected_checksum is not None:
        try:
            validate_sha256_checksum(expected_checksum)
        except RepositoryError as exc:
            raise ArtifactError("typed artifact expected checksum is malformed") from exc
        if expected_checksum != manifest_checksum:
            raise ArtifactError("typed artifact manifest checksum mismatch")
    manifest = _parse_canonical_json_object(
        manifest_raw,
        description="typed artifact manifest",
    )
    if set(manifest) != {
        "manifest_version",
        "artifact_id",
        "artifact_kind",
        "schema_version",
        "datasets",
    }:
        raise ArtifactError("typed artifact manifest fields are not exact")
    if manifest["manifest_version"] != TYPED_ARTIFACT_MANIFEST_VERSION:
        raise ArtifactError("unsupported typed artifact manifest version")
    if manifest["artifact_kind"] != expected_kind:
        raise ArtifactError("typed artifact kind mismatch")
    if manifest["schema_version"] != expected_schema_version:
        raise ArtifactError("typed artifact schema version mismatch")
    entries_value = manifest["datasets"]
    if not isinstance(entries_value, list) or len(entries_value) != len(layout):
        raise ArtifactError("typed artifact dataset declarations are malformed")
    verified: list[TypedDataset] = []
    identity_entries: list[JsonValue] = []
    for expected_name, raw_entry in zip(layout, entries_value, strict=True):
        if not isinstance(raw_entry, dict) or set(raw_entry) != {
            "name",
            "filename",
            "schema_version",
            "id_field",
            "row_count",
            "sha256",
        }:
            raise ArtifactError("typed artifact dataset declaration fields are not exact")
        entry = raw_entry
        name = entry["name"]
        filename = entry["filename"]
        schema = entry["schema_version"]
        id_field = entry["id_field"]
        row_count = entry["row_count"]
        digest = entry["sha256"]
        if (
            name != expected_name
            or filename != f"{expected_name}.jsonl"
            or id_field != _TYPED_ID_FIELDS[expected_name]
            or type(schema) is not str
            or not schema
            or type(row_count) is not int
            or row_count < 0
            or type(digest) is not str
        ):
            raise ArtifactError("typed artifact dataset declaration is invalid")
        try:
            validate_sha256_checksum(digest)
        except RepositoryError as exc:
            raise ArtifactError("typed artifact dataset checksum is malformed") from exc
        raw = (directory / filename).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ArtifactError(f"typed artifact dataset checksum mismatch: {expected_name}")
        rows = _parse_canonical_jsonl(raw, dataset_name=expected_name)
        if len(rows) != row_count:
            raise ArtifactError(f"typed artifact row count mismatch: {expected_name}")
        canonical_rows = _canonical_dataset_rows(
            expected_name,
            rows,
            id_field=id_field,
        )
        if rows != canonical_rows:
            raise ArtifactError(f"typed artifact rows are not canonically ordered: {expected_name}")
        verified.append(
            TypedDataset(
                name=expected_name,
                filename=filename,
                schema_version=schema,
                id_field=id_field,
                row_count=row_count,
                checksum_sha256=digest,
                rows=rows,
            )
        )
        identity_entries.append(entry)
    identity_payload: dict[str, JsonValue] = {
        "artifact_kind": expected_kind,
        "schema_version": expected_schema_version,
        "datasets": identity_entries,
    }
    artifact_id = content_addressed_id(
        identity_type=TYPED_ARTIFACT_MANIFEST_VERSION,
        payload=identity_payload,
    )
    if manifest["artifact_id"] != artifact_id:
        raise ArtifactError("typed artifact id does not match dataset content")
    if expected_artifact_id is not None and expected_artifact_id != artifact_id:
        raise ArtifactError("typed artifact id does not match expected id")
    return TypedAnalyticalArtifact(
        relative_directory=relative_directory,
        artifact_id=artifact_id,
        artifact_kind=expected_kind,
        schema_version=expected_schema_version,
        checksum_sha256=manifest_checksum,
        datasets=tuple(verified),
    )


def _typed_layout(kind: str) -> tuple[str, ...]:
    try:
        return TYPED_DATASET_LAYOUTS[kind]
    except KeyError as exc:
        raise ArtifactError("typed artifact kind must be analysis or backtest") from exc


def _canonical_dataset_rows(
    name: str,
    rows: tuple[dict[str, JsonValue], ...],
    *,
    id_field: str,
) -> tuple[dict[str, JsonValue], ...]:
    canonical: list[dict[str, JsonValue]] = []
    identifiers: set[str] = set()
    for raw_row in rows:
        try:
            value = ensure_json_value(raw_row)
        except RepositoryError as exc:
            raise ArtifactError(f"{name} contains a non-JSON row") from exc
        if not isinstance(value, dict):
            raise ArtifactError(f"{name} rows must be JSON objects")
        row = value
        identifier = row.get(id_field)
        if type(identifier) is not str or not identifier:
            raise ArtifactError(f"{name} row has an invalid {id_field}")
        if identifier in identifiers:
            raise ArtifactError(f"{name} contains duplicate {id_field}")
        identifiers.add(identifier)
        _validate_typed_row(name, row)
        canonical.append(cast(dict[str, JsonValue], json.loads(dumps_canonical_json(row))))
    return tuple(sorted(canonical, key=lambda row: cast(str, row[id_field])))


def _validate_typed_row(name: str, row: dict[str, JsonValue]) -> None:
    if name == "predictions":
        probabilities = row.get("probabilities")
        ordered = row.get("ordered_selection_ids")
        if not isinstance(probabilities, list) or not 2 <= len(probabilities) <= 4:
            raise ArtifactError("prediction row requires 2, 3, or 4 probabilities")
        if not isinstance(ordered, list) or len(ordered) != len(probabilities):
            raise ArtifactError("prediction ordered selection space is malformed")
        values: list[float] = []
        probability_ids: list[str] = []
        for item in probabilities:
            if not isinstance(item, dict):
                raise ArtifactError("prediction probability entry is malformed")
            selection_id = item.get("selection_id")
            probability = item.get("probability")
            if type(selection_id) is not str or not _finite_number(probability):
                raise ArtifactError("prediction probability entry is malformed")
            probability_ids.append(selection_id)
            values.append(float(cast(float | int, probability)))
        if probability_ids != ordered or abs(math.fsum(values) - 1.0) > 1e-9:
            raise ArtifactError("prediction probability space is incomplete or unordered")
    if name in {"market_evaluations", "opportunities", "combinations"}:
        if not _finite_number(row.get("expected_value")):
            raise ArtifactError(f"{name} row expected_value must be finite")
    if name == "opportunities":
        required_lineage = (
            "model_artifact_id",
            "model_checksum_sha256",
            "model_specification_version",
            "feature_artifact_id",
            "feature_manifest_checksum_sha256",
            "feature_specification_version",
            "feature_row_id",
        )
        if any(type(row.get(field)) is not str or not row.get(field) for field in required_lineage):
            raise ArtifactError("opportunity row lineage is incomplete")
        _validate_decision_timing(row)
    if name == "combinations":
        probability = row.get("joint_probability")
        odds = row.get("total_decimal_odds")
        expected_value = row.get("expected_value")
        if (
            not _finite_number(probability)
            or not 0 <= float(cast(float | int, probability)) <= 1
            or not _finite_number(odds)
            or float(cast(float | int, odds)) <= 1
        ):
            raise ArtifactError("combination probability or odds is invalid")
        calculated = float(cast(float | int, probability)) * float(cast(float | int, odds)) - 1
        if abs(calculated - float(cast(float | int, expected_value))) > 1e-9:
            raise ArtifactError("combination expected_value is inconsistent")
        _validate_decision_timing(
            row,
            decision_field="common_decision_time_utc",
            start_field="earliest_event_start_utc",
            strict=True,
        )
    if name == "settlements" and row.get("result") not in {"win", "loss"}:
        raise ArtifactError("v1 settlement rows may only contain win or loss")


def _validate_decision_timing(
    row: dict[str, JsonValue],
    *,
    decision_field: str = "decision_as_of_utc",
    start_field: str = "event_start_utc",
    strict: bool = False,
) -> None:
    decision = row.get(decision_field)
    start = row.get(start_field)
    if type(decision) is not str or type(start) is not str:
        raise ArtifactError("decision timing fields are incomplete")
    try:
        decision_time = datetime.fromisoformat(decision.replace("Z", "+00:00"))
        start_time = datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArtifactError("decision timing field is malformed") from exc
    if decision_time > start_time or (strict and decision_time >= start_time):
        raise ArtifactError("decision timing follows event start")


def _parse_canonical_json_object(
    raw: bytes,
    *,
    description: str,
) -> dict[str, JsonValue]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"{description} is malformed") from exc
    if not isinstance(parsed, dict):
        raise ArtifactError(f"{description} must be a JSON object")
    try:
        canonical = (dumps_canonical_json(ensure_json_value(parsed)) + "\n").encode("utf-8")
    except RepositoryError as exc:
        raise ArtifactError(f"{description} is not canonical JSON") from exc
    if raw != canonical:
        raise ArtifactError(f"{description} bytes are not canonical")
    return cast(dict[str, JsonValue], ensure_json_value(parsed))


def _parse_canonical_jsonl(
    raw: bytes,
    *,
    dataset_name: str,
) -> tuple[dict[str, JsonValue], ...]:
    if raw and not raw.endswith(b"\n"):
        raise ArtifactError(f"{dataset_name} JSONL must end with a newline")
    rows: list[dict[str, JsonValue]] = []
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ArtifactError(f"{dataset_name} JSONL is not UTF-8") from exc
    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ArtifactError(f"{dataset_name} JSONL is malformed") from exc
        if not isinstance(parsed, dict) or dumps_canonical_json(parsed) != line:
            raise ArtifactError(f"{dataset_name} JSONL row is not canonical")
        rows.append(cast(dict[str, JsonValue], ensure_json_value(parsed)))
    return tuple(rows)


def _one_checksum(path: Path) -> str:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) != 1:
        raise ArtifactError("typed artifact checksum sidecar must contain one digest")
    try:
        return validate_sha256_checksum(lines[0])
    except RepositoryError as exc:
        raise ArtifactError("typed artifact checksum sidecar is malformed") from exc


def _finite_number(value: JsonValue | None) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _verify_directory(
    directory: Path,
    *,
    expected_artifact_type: str,
    expected_schema_version: str,
    expected_checksum: str | None,
    expected_artifact_id: str | None = None,
    relative_directory: str = ".",
) -> AnalyticalArtifact:
    if directory.is_symlink() or not directory.is_dir():
        raise ArtifactError("analytical artifact path must be a real directory")
    entries = tuple(directory.iterdir())
    names = {item.name for item in entries}
    if names != ANALYTICAL_ARTIFACT_FILES:
        raise ArtifactError("analytical artifact directory has missing or extra files")
    if any(item.is_symlink() or not item.is_file() for item in entries):
        raise ArtifactError("analytical artifact files must be regular files, not symlinks")
    manifest_path = directory / ANALYTICAL_MANIFEST_FILENAME
    checksum_path = directory / ANALYTICAL_CHECKSUM_FILENAME
    checksum_lines = [
        item.strip()
        for item in checksum_path.read_text(encoding="utf-8").splitlines()
        if item.strip()
    ]
    if len(checksum_lines) != 1:
        raise ArtifactError("analytical artifact checksum sidecar must contain one digest")
    try:
        sidecar_checksum = validate_sha256_checksum(checksum_lines[0])
        if expected_checksum is not None:
            validate_sha256_checksum(expected_checksum)
    except RepositoryError as exc:
        raise ArtifactError("analytical artifact checksum is malformed") from exc
    raw = manifest_path.read_bytes()
    actual_checksum = hashlib.sha256(raw).hexdigest()
    if actual_checksum != sidecar_checksum:
        raise ArtifactError("analytical artifact checksum sidecar mismatch")
    if expected_checksum is not None and actual_checksum != expected_checksum:
        raise ArtifactError("analytical artifact checksum does not match expected checksum")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactError("analytical artifact manifest is malformed") from exc
    if not isinstance(document, dict) or set(document) != {
        "manifest_version",
        "artifact_type",
        "schema_version",
        "artifact_id",
        "payload",
    }:
        raise ArtifactError("analytical artifact manifest fields are not exact")
    try:
        canonical_raw = (dumps_canonical_json(ensure_json_value(document)) + "\n").encode("utf-8")
    except RepositoryError as exc:
        raise ArtifactError("analytical artifact manifest is not canonical JSON") from exc
    if raw != canonical_raw:
        raise ArtifactError("analytical artifact manifest bytes are not canonical")
    if document["manifest_version"] != ANALYTICAL_ARTIFACT_MANIFEST_VERSION:
        raise ArtifactError("unsupported analytical artifact manifest version")
    if document["artifact_type"] != expected_artifact_type:
        raise ArtifactError("analytical artifact type mismatch")
    if document["schema_version"] != expected_schema_version:
        raise ArtifactError("analytical artifact schema version mismatch")
    payload = ensure_json_value(document["payload"])
    expected_id = content_addressed_id(
        identity_type=(f"analytical-artifact:{expected_artifact_type}:{expected_schema_version}"),
        payload={"payload": payload},
    )
    if document["artifact_id"] != expected_id:
        raise ArtifactError("analytical artifact id does not match its content")
    if expected_artifact_id is not None and expected_artifact_id != expected_id:
        raise ArtifactError("analytical artifact id does not match expected id")
    return AnalyticalArtifact(
        relative_directory=relative_directory,
        artifact_id=expected_id,
        artifact_type=expected_artifact_type,
        schema_version=expected_schema_version,
        checksum_sha256=actual_checksum,
        payload=payload,
    )


def _resolve(root: Path, relative_path: str, *, expect_file: bool) -> Path:
    try:
        return resolve_under_root(
            root,
            relative_path,
            expect_file=expect_file,
            error_type=ArtifactError,
        )
    except RepositoryError as exc:
        raise ArtifactError(str(exc)) from exc
