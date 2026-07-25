"""Sport-agnostic model contracts for explicit, pickle-free artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sports_analytics.core.exceptions import ModelError
from sports_analytics.features.contracts import OutcomeSpace

MODEL_MANIFEST_VERSION: Final[str] = "model-manifest-v1"
MODEL_CHECKSUM_SIDECAR: Final[str] = "model_checksum.sha256"


@dataclass(frozen=True, slots=True)
class ModelSpecification:
    """Versioned description of one trainable model family."""

    model_specification_version: str
    sport_code: str
    market_key: str
    algorithm: str
    outcome_space: OutcomeSpace
    feature_specification_version: str
    ordered_feature_names: tuple[str, ...]
    description: str

    def __post_init__(self) -> None:
        if not self.outcome_space.ordered_labels:
            msg = "model specification requires a non-empty outcome space"
            raise ModelError(msg)
        if not self.ordered_feature_names:
            msg = "model specification requires ordered feature names"
            raise ModelError(msg)
        if len(set(self.ordered_feature_names)) != len(self.ordered_feature_names):
            msg = "model specification feature names must be unique and ordered"
            raise ModelError(msg)


@dataclass(frozen=True, slots=True)
class ProbabilityPrediction:
    """Calibrated probability forecast for one ordered outcome space."""

    outcome_space: OutcomeSpace
    probabilities: dict[str, float]

    def ordered_values(self) -> tuple[float, ...]:
        """Return probabilities in canonical outcome order."""
        return tuple(self.probabilities[label] for label in self.outcome_space.ordered_labels)
