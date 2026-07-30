"""Closed-loop immutable training-evidence and challenger governance contracts."""

from sports_analytics.learning.lifecycle import (
    ChampionHistory,
    ChampionRevision,
    RetrainingDecision,
    RetrainingPolicy,
    TrainingEligibilityRecord,
    TrainingEligibilityState,
    build_training_eligibility_ledger,
    evaluate_retraining_trigger,
    promote_challenger,
    rollback_champion,
)
from sports_analytics.learning.offline_proof import (
    OfflineClosedLoopProof,
    load_offline_closed_loop_proof,
    write_offline_closed_loop_proof,
)

__all__ = [
    "ChampionHistory",
    "ChampionRevision",
    "OfflineClosedLoopProof",
    "RetrainingDecision",
    "RetrainingPolicy",
    "TrainingEligibilityRecord",
    "TrainingEligibilityState",
    "build_training_eligibility_ledger",
    "evaluate_retraining_trigger",
    "load_offline_closed_loop_proof",
    "promote_challenger",
    "rollback_champion",
    "write_offline_closed_loop_proof",
]
