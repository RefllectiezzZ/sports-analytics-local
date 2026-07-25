"""Reusable feature-engineering contracts for future sports and markets.

This package currently ships one production feature specification:
``football-1x2-prematch-features-v1``. Contracts stay sport-agnostic so later
participant-scoped features can reuse the same artifact and metadata shapes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from sports_analytics.core.exceptions import FeatureError

FEATURE_MANIFEST_VERSION: Final[str] = "feature-manifest-v1"
FEATURE_SCOPE_TEAM: Final[str] = "team"
FEATURE_SCOPE_PARTICIPANT: Final[str] = "participant"

FOOTBALL_1X2_PREMATCH_FEATURES_V1: Final[str] = "football-1x2-prematch-features-v1"

#: Ordered model-feature whitelist. Never discover features by scanning numeric columns.
FOOTBALL_1X2_FEATURE_NAMES_V1: Final[tuple[str, ...]] = (
    "home_elo",
    "away_elo",
    "elo_diff",
    "home_matches_played",
    "away_matches_played",
    "home_ppg_5",
    "home_ppg_10",
    "away_ppg_5",
    "away_ppg_10",
    "home_gf_pm_5",
    "home_gf_pm_10",
    "away_gf_pm_5",
    "away_gf_pm_10",
    "home_ga_pm_5",
    "home_ga_pm_10",
    "away_ga_pm_5",
    "away_ga_pm_10",
    "home_gd_pm_5",
    "home_gd_pm_10",
    "away_gd_pm_5",
    "away_gd_pm_10",
    "home_home_ppg_5",
    "away_away_ppg_5",
    "home_days_since_prev",
    "away_days_since_prev",
    "rest_day_diff",
    "home_window5_available",
    "home_window10_available",
    "away_window5_available",
    "away_window10_available",
    "home_home_form_available",
    "away_away_form_available",
    "home_rest_available",
    "away_rest_available",
)

FOOTBALL_1X2_METADATA_COLUMNS: Final[tuple[str, ...]] = (
    "canonical_event_id",
    "competition_id",
    "season_id",
    "event_date",
    "scheduled_start_utc",
    "feature_cutoff_date",
    "feature_specification_version",
    "home_canonical_participant_id",
    "away_canonical_participant_id",
)

#: Fields that must never appear in the model feature whitelist.
FORBIDDEN_MODEL_FEATURE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "home_score",
        "away_score",
        "result_code",
        "decimal_odds",
        "implied_probability",
        "opening_odds",
        "closing_odds",
        "market_implied_probability",
        "post_match",
        "HTHG",
        "HTAG",
        "HTR",
        "AvgH",
        "AvgD",
        "AvgA",
        "AvgCH",
        "AvgCD",
        "AvgCA",
        "B365H",
        "B365D",
        "B365A",
        "B365CH",
        "B365CD",
        "B365CA",
    }
)


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

    def __post_init__(self) -> None:
        if not self.ordered_feature_names:
            msg = "feature specification requires at least one feature name"
            raise FeatureError(msg)
        if len(set(self.ordered_feature_names)) != len(self.ordered_feature_names):
            msg = "feature names must be unique and ordered"
            raise FeatureError(msg)
        forbidden = FORBIDDEN_MODEL_FEATURE_FIELDS.intersection(self.ordered_feature_names)
        if forbidden:
            msg = f"feature whitelist contains forbidden fields: {sorted(forbidden)}"
            raise FeatureError(msg)
        if self.feature_scope not in {FEATURE_SCOPE_TEAM, FEATURE_SCOPE_PARTICIPANT}:
            msg = f"unsupported feature scope: {self.feature_scope}"
            raise FeatureError(msg)


@dataclass(frozen=True, slots=True)
class SnapshotIdentity:
    """Immutable identity of one training input snapshot."""

    snapshot_id: str
    relative_manifest_path: str
    manifest_checksum_sha256: str
    schema_version: str
    schema_fingerprint_events: str
    competition_id: str
    season_id: str
    season_label: str
    sport_code: str
    source_name: str
    event_row_count: int


@dataclass(frozen=True, slots=True)
class FeatureRowMetadata:
    """Non-feature metadata attached to every feature row."""

    canonical_event_id: str
    competition_id: str
    season_id: str
    event_date: date
    scheduled_start_utc: datetime | None
    feature_cutoff_date: date
    feature_specification_version: str
    home_canonical_participant_id: str
    away_canonical_participant_id: str


def football_1x2_prematch_specification() -> FeatureSpecification:
    """Return the v1 team-level football full-match 1X2 pre-match specification."""
    return FeatureSpecification(
        specification_version=FOOTBALL_1X2_PREMATCH_FEATURES_V1,
        sport_code="football",
        market_key="football.match-result.1x2.full-match",
        feature_scope=FEATURE_SCOPE_TEAM,
        ordered_feature_names=FOOTBALL_1X2_FEATURE_NAMES_V1,
        metadata_columns=FOOTBALL_1X2_METADATA_COLUMNS,
        description=(
            "Team-level pre-match features for football full-match 1X2. "
            "Participant-scoped features (players, lineups, injuries) are reserved "
            "for a later specification and are not present in v1."
        ),
    )


def validate_feature_vector(
    *,
    feature_names: tuple[str, ...],
    values: tuple[float, ...],
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
    if feature_names != FOOTBALL_1X2_FEATURE_NAMES_V1:
        msg = "feature names must match the ordered whitelist exactly"
        raise FeatureError(msg)
    if len(values) != len(feature_names):
        msg = f"feature vector length mismatch: expected {len(feature_names)}, got {len(values)}"
        raise FeatureError(msg)
