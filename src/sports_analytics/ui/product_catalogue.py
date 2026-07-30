"""Strict discovery of persisted football product read models."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sports_analytics.artifacts import (
    ANALYTICAL_MANIFEST_FILENAME,
    AnalyticalArtifact,
    load_analytical_artifact,
)
from sports_analytics.core.exceptions import ArtifactError
from sports_analytics.services.football_product import (
    FOOTBALL_PRODUCT_READ_MODEL_SCHEMA,
    FOOTBALL_PRODUCT_READ_MODEL_TYPE,
)


@dataclass(frozen=True, slots=True)
class ProductReadModelEntry:
    relative_directory: str
    artifact_id: str | None
    checksum_sha256: str | None
    validation_error: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.validation_error is None


def discover_product_read_models(root: Path) -> tuple[ProductReadModelEntry, ...]:
    resolved = root.resolve()
    if not resolved.is_dir():
        return ()
    entries: list[ProductReadModelEntry] = []
    for manifest in sorted(resolved.rglob(ANALYTICAL_MANIFEST_FILENAME)):
        if not manifest.is_file() or manifest.is_symlink():
            continue
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(raw, dict)
            or raw.get("artifact_type") != FOOTBALL_PRODUCT_READ_MODEL_TYPE
        ):
            continue
        relative = manifest.parent.relative_to(resolved).as_posix()
        try:
            artifact = load_analytical_artifact(
                root=resolved,
                relative_directory=relative,
                expected_artifact_type=FOOTBALL_PRODUCT_READ_MODEL_TYPE,
                expected_schema_version=FOOTBALL_PRODUCT_READ_MODEL_SCHEMA,
            )
            _validate_read_model(artifact)
        except (ArtifactError, OSError, ValueError) as exc:
            entries.append(
                ProductReadModelEntry(
                    relative_directory=relative,
                    artifact_id=None,
                    checksum_sha256=None,
                    validation_error=str(exc) or type(exc).__name__,
                )
            )
        else:
            entries.append(
                ProductReadModelEntry(
                    relative_directory=relative,
                    artifact_id=artifact.artifact_id,
                    checksum_sha256=artifact.checksum_sha256,
                )
            )
    return tuple(sorted(entries, key=lambda item: item.relative_directory))


def load_product_read_model(
    *,
    root: Path,
    entry: ProductReadModelEntry,
) -> AnalyticalArtifact:
    if not entry.is_valid or entry.artifact_id is None or entry.checksum_sha256 is None:
        raise ArtifactError("invalid football product read models cannot be selected")
    artifact = load_analytical_artifact(
        root=root,
        relative_directory=entry.relative_directory,
        expected_artifact_type=FOOTBALL_PRODUCT_READ_MODEL_TYPE,
        expected_schema_version=FOOTBALL_PRODUCT_READ_MODEL_SCHEMA,
        expected_checksum=entry.checksum_sha256,
        expected_artifact_id=entry.artifact_id,
    )
    _validate_read_model(artifact)
    return artifact


def _validate_read_model(artifact: AnalyticalArtifact) -> None:
    payload = artifact.payload
    if not isinstance(payload, dict) or set(payload) != {
        "model_status",
        "artifact_lineage",
        "product_state",
        "market_capabilities",
    }:
        raise ArtifactError("football product read-model fields are not exact")
    product = payload["product_state"]
    if not isinstance(product, dict):
        raise ArtifactError("football product state is invalid")
    if product.get("placement_state") != "manual-only":
        raise ArtifactError("football product must remain manual placement only")
    if product.get("automatic_bookmaker_access") is not False:
        raise ArtifactError("football product read model claims forbidden bookmaker access")
    eligibility = product.get("eligibility")
    if not isinstance(eligibility, dict) or set(eligibility) != {
        "model_artifact_valid",
        "fair_odds_eligible",
        "opportunity_analysis_eligible",
        "bet_proposal_eligible",
        "promotion_eligible",
    }:
        raise ArtifactError("football product eligibility states are not exact")
    if any(type(value) is not bool for value in eligibility.values()):
        raise ArtifactError("football product eligibility states must be boolean")
    capabilities = payload["market_capabilities"]
    if not isinstance(capabilities, list) or not capabilities:
        raise ArtifactError("football product capability matrix is absent")
