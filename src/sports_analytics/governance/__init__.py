"""Verified model registry and champion-challenger governance."""

from sports_analytics.governance.contracts import (
    GovernanceDecision,
    GovernanceDecisionKind,
    ModelEvaluationEvidence,
    ModelLifecycleStatus,
    ModelRegistryEntry,
    ModelRole,
    PromotionPolicy,
    evaluate_challenger,
)
from sports_analytics.governance.repository import ModelGovernanceRepository

__all__ = [
    "GovernanceDecision",
    "GovernanceDecisionKind",
    "ModelEvaluationEvidence",
    "ModelGovernanceRepository",
    "ModelLifecycleStatus",
    "ModelRegistryEntry",
    "ModelRole",
    "PromotionPolicy",
    "evaluate_challenger",
]
