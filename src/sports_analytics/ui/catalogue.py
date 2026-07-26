"""Deterministic discovery and strict loading of typed analytical artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sports_analytics.artifacts import (
    ANALYTICAL_MANIFEST_FILENAME,
    TypedAnalyticalArtifact,
    load_typed_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError


@dataclass(frozen=True, slots=True)
class ArtifactCatalogueEntry:
    """One trusted typed artifact or one explicitly reported invalid candidate."""

    relative_directory: str
    artifact_kind: str | None
    schema_version: str | None
    artifact_id: str | None
    checksum_sha256: str | None
    dataset_row_counts: tuple[tuple[str, int], ...]
    validation_error: str | None = None

    @property
    def is_valid(self) -> bool:
        """Return whether this entry came from full typed artifact verification."""
        return self.validation_error is None


def discover_typed_artifacts(root: Path) -> tuple[ArtifactCatalogueEntry, ...]:
    """Discover typed artifact directories and report every invalid candidate.

    Candidate manifests are read only for the kind and schema needed to invoke
    the strict public loader. No dataset rows are exposed until that loader has
    verified the manifest, checksums, identities, schemas, and cross-dataset
    integrity.
    """
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        return ()
    candidates = sorted(
        (
            path
            for path in resolved_root.rglob(ANALYTICAL_MANIFEST_FILENAME)
            if path.is_file() and not path.is_symlink()
        ),
        key=lambda path: path.parent.relative_to(resolved_root).as_posix(),
    )
    entries = [_inspect_candidate(resolved_root, manifest) for manifest in candidates]
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                entry.artifact_kind or "~invalid",
                entry.schema_version or "",
                entry.relative_directory,
                entry.artifact_id or "",
            ),
        )
    )


def load_catalogue_artifact(
    *,
    root: Path,
    entry: ArtifactCatalogueEntry,
) -> TypedAnalyticalArtifact:
    """Strictly reload one trusted catalogue selection by immutable identity."""
    if (
        not entry.is_valid
        or entry.artifact_kind is None
        or entry.schema_version is None
        or entry.artifact_id is None
        or entry.checksum_sha256 is None
    ):
        raise ArtifactError("invalid catalogue entries cannot be selected")
    return load_typed_analytical_artifact(
        root=root,
        relative_directory=entry.relative_directory,
        expected_kind=entry.artifact_kind,
        expected_schema_version=entry.schema_version,
        expected_checksum=entry.checksum_sha256,
        expected_artifact_id=entry.artifact_id,
    )


def _inspect_candidate(root: Path, manifest_path: Path) -> ArtifactCatalogueEntry:
    relative = manifest_path.parent.relative_to(root).as_posix()
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ArtifactError("manifest must be a JSON object")
        kind = _manifest_string(raw, "artifact_kind")
        schema = _manifest_string(raw, "schema_version")
        if kind not in {"analysis", "backtest"}:
            raise ArtifactError("manifest is not a supported typed artifact")
        artifact = load_typed_analytical_artifact(
            root=root,
            relative_directory=relative,
            expected_kind=kind,
            expected_schema_version=schema,
        )
    except (ArtifactError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return ArtifactCatalogueEntry(
            relative_directory=relative,
            artifact_kind=None,
            schema_version=None,
            artifact_id=None,
            checksum_sha256=None,
            dataset_row_counts=(),
            validation_error=_safe_error(exc),
        )
    return _entry_from_artifact(artifact)


def _manifest_string(manifest: dict[str, Any], field: str) -> str:
    value = manifest.get(field)
    if type(value) is not str or not value:
        raise ArtifactError(f"typed artifact manifest has invalid {field}")
    return value


def _entry_from_artifact(artifact: TypedAnalyticalArtifact) -> ArtifactCatalogueEntry:
    return ArtifactCatalogueEntry(
        relative_directory=artifact.relative_directory,
        artifact_kind=artifact.artifact_kind,
        schema_version=artifact.schema_version,
        artifact_id=artifact.artifact_id,
        checksum_sha256=artifact.checksum_sha256,
        dataset_row_counts=tuple(
            (dataset.name, dataset.row_count) for dataset in artifact.datasets
        ),
    )


def _safe_error(exc: BaseException) -> str:
    message = str(exc).strip()
    return message or type(exc).__name__
