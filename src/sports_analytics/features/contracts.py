"""Sport-agnostic feature-engineering contracts.

Shared contracts intentionally avoid sport-specific outcome labels, feature
names, or competition semantics. Sport implementations provide concrete
``FeatureSpecification`` and ``TargetSpecification`` values in their own
packages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final

from sports_analytics.core.exceptions import FeatureError

FEATURE_MANIFEST_VERSION: Final[str] = "feature-manifest-v1"
FEATURE_SCOPE_VERSION_PREFIX: Final[str] = "feature-scope-v1:"

FEATURE_ARTIFACT_FILES: Final[frozenset[str]] = frozenset(
    {"features.parquet", "targets.parquet", "folds.parquet", "manifest.json"}
)
FEATURE_CHECKSUM_SIDECAR: Final[str] = "manifest_checksum.sha256"
PROBABILITY_SUM_TOLERANCE: Final[float] = 1e-9

#: Universal leakage fields that must never appear in any model feature whitelist.
UNIVERSAL_FORBIDDEN_MODEL_FEATURE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "result_code",
        "decimal_odds",
        "implied_probability",
        "opening_odds",
        "closing_odds",
        "market_implied_probability",
        "post_match",
    }
)

_FEATURE_SCOPE_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9-]*$")


def validate_feature_scope_key(feature_scope: str) -> None:
    """Validate a versioned, extensible feature-scope identifier."""
    if not feature_scope.startswith(FEATURE_SCOPE_VERSION_PREFIX):
        msg = f"unsupported feature scope version: {feature_scope}"
        raise FeatureError(msg)
    scope_key = feature_scope[len(FEATURE_SCOPE_VERSION_PREFIX) :]
    if not scope_key or _FEATURE_SCOPE_KEY_PATTERN.fullmatch(scope_key) is None:
        msg = f"invalid feature scope key: {feature_scope}"
        raise FeatureError(msg)


@dataclass(frozen=True, slots=True)
class OutcomeSpace:
    """Ordered outcome labels for one supervised target space."""

    ordered_labels: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.ordered_labels:
            msg = "outcome space requires at least one ordered label"
            raise FeatureError(msg)
        if len(set(self.ordered_labels)) != len(self.ordered_labels):
            msg = "outcome labels must be unique and ordered"
            raise FeatureError(msg)

    def index(self, label: str) -> int:
        """Return the canonical index for ``label``."""
        try:
            return self.ordered_labels.index(label)
        except ValueError as exc:
            msg = f"unsupported outcome label: {label}"
            raise FeatureError(msg) from exc


@dataclass(frozen=True, slots=True)
class TargetSpecification:
    """Versioned supervised target contract."""

    specification_version: str
    outcome_space: OutcomeSpace
    target_column: str
    description: str


@dataclass(frozen=True, slots=True)
class FeatureSpecification:
    """Versioned description of one feature matrix contract."""

    specification_version: str
    sport_code: str
    market_key: str
    feature_scope: str
    ordered_feature_names: tuple[str, ...]
    metadata_columns: tuple[str, ...]
    description: str
    forbidden_feature_names: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        validate_feature_scope_key(self.feature_scope)
        if not self.ordered_feature_names:
            msg = "feature specification requires at least one feature name"
            raise FeatureError(msg)
        if len(set(self.ordered_feature_names)) != len(self.ordered_feature_names):
            msg = "feature names must be unique and ordered"
            raise FeatureError(msg)
        forbidden = UNIVERSAL_FORBIDDEN_MODEL_FEATURE_FIELDS.union(self.forbidden_feature_names)
        overlap = forbidden.intersection(self.ordered_feature_names)
        if overlap:
            msg = f"feature whitelist contains forbidden fields: {sorted(overlap)}"
            raise FeatureError(msg)


@dataclass(frozen=True, slots=True)
class SupervisedExample:
    """One labelled example for temporal validation and training."""

    example_id: str
    event_date: date
    target_label: str


@dataclass(frozen=True, slots=True)
class SnapshotIdentity:
    """Immutable identity of one training input snapshot."""

    snapshot_id: str
    relative_manifest_path: str
    manifest_checksum_sha256: str
    schema_version: str
    schema_fingerprint_events: str
    scope_id: str
    partition_label: str
    sport_code: str
    source_name: str
    event_row_count: int


@dataclass(frozen=True, slots=True)
class FeatureRowMetadata:
    """Non-feature metadata attached to every feature row."""

    canonical_event_id: str
    event_date: date
    scheduled_start_utc: datetime | None
    feature_cutoff_date: date
    feature_specification_version: str
    scope_metadata: dict[str, str]


def validate_feature_vector(
    *,
    feature_names: tuple[str, ...],
    values: tuple[float, ...],
    expected_names: tuple[str, ...],
    expected_specification_version: str,
    provided_specification_version: str,
) -> None:
    """Reject feature-version mismatches and reordered or incomplete vectors."""
    if provided_specification_version != expected_specification_version:
        msg = (
            "feature specification version mismatch: "
            f"expected {expected_specification_version}, got {provided_specification_version}"
        )
        raise FeatureError(msg)
    if feature_names != expected_names:
        msg = "feature names must match the ordered whitelist exactly"
        raise FeatureError(msg)
    if len(values) != len(feature_names):
        msg = f"feature vector length mismatch: expected {len(feature_names)}, got {len(values)}"
        raise FeatureError(msg)
