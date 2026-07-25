"""Football-specific feature and target specifications."""

from __future__ import annotations

from typing import Final

from sports_analytics.features.contracts import (
    FeatureSpecification,
    OutcomeSpace,
    TargetSpecification,
    validate_feature_vector,
)

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
    "home_window5_count",
    "home_window10_count",
    "away_window5_count",
    "away_window10_count",
    "home_home_form_count",
    "away_away_form_count",
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

FOOTBALL_1X2_OUTCOME_SPACE: Final[OutcomeSpace] = OutcomeSpace(
    ordered_labels=("home", "draw", "away")
)


def football_1x2_prematch_specification() -> FeatureSpecification:
    """Return the v1 team-level football full-match 1X2 pre-match specification."""
    return FeatureSpecification(
        specification_version=FOOTBALL_1X2_PREMATCH_FEATURES_V1,
        sport_code="football",
        market_key="football.match-result.1x2.full-match",
        feature_scope="team",
        ordered_feature_names=FOOTBALL_1X2_FEATURE_NAMES_V1,
        metadata_columns=FOOTBALL_1X2_METADATA_COLUMNS,
        description=(
            "Team-level pre-match features for football full-match 1X2. "
            "Participant-scoped features (players, lineups, injuries) are reserved "
            "for a later specification and are not present in v1."
        ),
    )


def football_1x2_target_specification() -> TargetSpecification:
    """Return the supervised target contract for football full-match 1X2."""
    return TargetSpecification(
        specification_version="football-1x2-target-v1",
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        target_column="result_code",
        description="Full-match 1X2 result labels for finished football events.",
    )


def validate_football_1x2_feature_vector(
    *,
    feature_names: tuple[str, ...],
    values: tuple[float, ...],
    provided_specification_version: str,
) -> None:
    """Validate one football 1X2 feature vector against the v1 whitelist."""
    validate_feature_vector(
        feature_names=feature_names,
        values=values,
        expected_names=FOOTBALL_1X2_FEATURE_NAMES_V1,
        expected_specification_version=FOOTBALL_1X2_PREMATCH_FEATURES_V1,
        provided_specification_version=provided_specification_version,
    )
