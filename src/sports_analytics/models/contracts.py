"""Reusable model contracts for explicit, pickle-free artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sports_analytics.core.exceptions import ModelError
from sports_analytics.sports.football.markets import MATCH_RESULT_1X2_OUTCOMES

MODEL_MANIFEST_VERSION: Final[str] = "model-manifest-v1"
FOOTBALL_1X2_LOGISTIC_MODEL_V1: Final[str] = "football-1x2-logistic-v1"
OUTCOME_LABELS_1X2: Final[tuple[str, ...]] = MATCH_RESULT_1X2_OUTCOMES

PROBABILITY_SUM_TOLERANCE: Final[float] = 1e-9


@dataclass(frozen=True, slots=True)
class ModelSpecification:
    """Versioned description of one trainable model family."""

    model_specification_version: str
    sport_code: str
    market_key: str
    algorithm: str
    outcome_labels: tuple[str, ...]
    feature_specification_version: str
    description: str

    def __post_init__(self) -> None:
        if self.outcome_labels != OUTCOME_LABELS_1X2:
            msg = "football 1X2 models require ordered outcomes home, draw, away"
            raise ModelError(msg)


def football_1x2_logistic_specification(feature_specification_version: str) -> ModelSpecification:
    """Return the v1 multinomial logistic specification for football 1X2."""
    return ModelSpecification(
        model_specification_version=FOOTBALL_1X2_LOGISTIC_MODEL_V1,
        sport_code="football",
        market_key="football.match-result.1x2.full-match",
        algorithm="multinomial-logistic-regression",
        outcome_labels=OUTCOME_LABELS_1X2,
        feature_specification_version=feature_specification_version,
        description=(
            "Deterministic multinomial logistic regression baseline for football "
            "full-match 1X2 with temperature calibration. Team-level only."
        ),
    )
