"""Closed canonical abstention-reason registry."""

from __future__ import annotations

from enum import StrEnum

from sports_analytics.core.exceptions import EvaluationError


class AbstentionReason(StrEnum):
    NO_PRODUCTION_CHAMPION = "no-production-champion"
    INSUFFICIENT_REAL_HISTORICAL_EVIDENCE = "insufficient-real-historical-evidence"
    INSUFFICIENT_COMPETITION_EVIDENCE = "insufficient-competition-evidence"
    STALE_CHAMPION = "stale-champion"
    RETRAINING_OVERDUE = "retraining-overdue"
    RESULT_EVIDENCE_INCOMPLETE = "result-evidence-incomplete"
    PLAYER_CONTEXT_STALE = "player-context-stale"
    PLAYER_CONTEXT_UNRESOLVED = "player-context-unresolved"
    PLAYER_TRAIN_SERVE_EQUIVALENCE_UNAVAILABLE = "player-train-serve-equivalence-unavailable"
    NO_PROSPECTIVE_TIMESTAMPED_EVIDENCE = "no-prospective-timestamped-evidence"
    HISTORICAL_CLOSING_ONLY_EVIDENCE = "historical-closing-only-evidence"
    NEGATIVE_HISTORICAL_CLOSING_BACKTEST = "negative-historical-closing-backtest"
    MARKET_BASELINE_MATERIALLY_BETTER = "market-baseline-materially-better"
    NO_PROSPECTIVE_SETTLEMENT_CYCLE = "no-prospective-settlement-cycle"
    MODEL_CALIBRATION_FAILED = "model-calibration-failed"
    BOOTSTRAP_UNCERTAINTY_UNAVAILABLE = "bootstrap-uncertainty-unavailable"
    BOOTSTRAP_INTERVAL_TOO_WIDE = "bootstrap-interval-too-wide"
    CANDIDATE_DISAGREEMENT_TOO_HIGH = "candidate-disagreement-too-high"
    RHO_STABILITY_WARNING = "rho-stability-warning"
    TAIL_MASS_DEGRADED = "tail-mass-degraded"
    QUOTE_STALE = "quote-stale"
    QUOTE_INCOMPLETE = "quote-incomplete"
    QUOTE_UNAVAILABLE = "quote-unavailable"
    MARKET_UNSUPPORTED = "market-unsupported"
    SPORT_MODEL_UNAVAILABLE = "sport-model-unavailable"
    EVENT_UNRESOLVED = "event-unresolved"
    EVENT_STARTED = "event-started"
    SETTLEMENT_UNKNOWN = "settlement-unknown"
    EDGE_INSUFFICIENT = "edge-insufficient"
    CONSERVATIVE_EV_INSUFFICIENT = "conservative-ev-insufficient"
    PROVIDER_MISMATCH = "provider-mismatch"
    DEPENDENCY_UNKNOWN = "dependency-unknown"
    OFFERED_ODDS_BELOW_MINIMUM = "offered-odds-below-minimum"
    OFFERED_ODDS_ABOVE_MAXIMUM = "offered-odds-above-maximum"
    SCORE_PROBABILITY_UNPRICED = "score-probability-unpriced"
    HISTORY_FALLBACK = "history-fallback"


def canonical_abstention_codes(values: tuple[str | AbstentionReason, ...]) -> tuple[str, ...]:
    """Validate, deduplicate, and canonically order persisted reason codes."""
    try:
        normalized = tuple(AbstentionReason(value).value for value in values)
    except ValueError as exc:
        raise EvaluationError("unknown abstention reason code") from exc
    return tuple(sorted(set(normalized)))
