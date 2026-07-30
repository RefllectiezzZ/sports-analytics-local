"""Deterministic operational MVP readiness state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MVPState(StrEnum):
    """The one exact current state of the local operator workflow."""

    INITIALIZING = "initializing"
    HISTORICAL_DATA_REQUIRED = "historical-data-required"
    MODEL_PREPARATION_REQUIRED = "model-preparation-required"
    UPCOMING_EVENTS_REQUIRED = "upcoming-events-required"
    CURRENT_ODDS_REQUIRED = "current-odds-required"
    ANALYSING = "analysing"
    CANDIDATES_AVAILABLE = "candidates-available"
    ECONOMICALLY_HELD = "economically-held"
    PLACEABLE_MANUAL_PROPOSALS_AVAILABLE = "placeable-manual-proposals-available"
    FAILED = "failed"


class SetupStepState(StrEnum):
    """Human-facing setup state for one bounded workflow step."""

    READY = "ready"
    ACTION_REQUIRED = "action required"
    RUNNING = "running"
    FAILED = "failed"
    OPTIONAL_HELD = "optional/held"


@dataclass(frozen=True, slots=True)
class MVPReadinessFacts:
    """Validated persisted facts used by the pure state transition function."""

    runtime_initialized: bool
    historical_snapshot_count: int = 0
    participant_registry_available: bool = False
    active_champion_count: int = 0
    upcoming_event_count: int = 0
    current_quote_count: int = 0
    analysis_running: bool = False
    analytical_candidate_count: int = 0
    held_candidate_count: int = 0
    placeable_manual_proposal_count: int = 0
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class MVPStatus:
    """Complete, UI-safe snapshot of current MVP readiness."""

    state: MVPState
    steps: tuple[tuple[str, SetupStepState], ...]
    blockers: tuple[str, ...]
    active_competitions: tuple[str, ...]
    active_models: tuple[str, ...]
    historical_snapshot_count: int
    upcoming_event_count: int
    current_quote_count: int
    matches_analysed: int
    analytical_candidate_count: int
    held_candidate_count: int
    rejected_candidate_count: int
    placeable_manual_proposal_count: int
    accumulator_count: int
    last_successful_analysis: str | None
    worker_state: str
    failure: str | None = None

    @property
    def next_required_action(self) -> str:
        """Return concise, deterministic operator guidance."""
        guidance = {
            MVPState.INITIALIZING: "Wait for local runtime initialization.",
            MVPState.HISTORICAL_DATA_REQUIRED: "Add verified Football-Data history.",
            MVPState.MODEL_PREPARATION_REQUIRED: "Prepare the governed production model.",
            MVPState.UPCOMING_EVENTS_REQUIRED: "Add upcoming matches.",
            MVPState.CURRENT_ODDS_REQUIRED: "Add a complete current offered market.",
            MVPState.ANALYSING: "Wait for the current analysis to finish.",
            MVPState.CANDIDATES_AVAILABLE: "Review analytical candidates.",
            MVPState.ECONOMICALLY_HELD: "Review candidates and their exact hold reasons.",
            MVPState.PLACEABLE_MANUAL_PROPOSALS_AVAILABLE: (
                "Review eligible proposals and place them manually."
            ),
            MVPState.FAILED: "Review the sanitized failure and retry the bounded action.",
        }
        return guidance[self.state]


def determine_mvp_state(facts: MVPReadinessFacts) -> MVPState:
    """Return the one exact state for already-validated persisted facts."""
    if facts.failure is not None:
        return MVPState.FAILED
    if not facts.runtime_initialized:
        return MVPState.INITIALIZING
    if facts.analysis_running:
        return MVPState.ANALYSING
    if facts.historical_snapshot_count == 0:
        return MVPState.HISTORICAL_DATA_REQUIRED
    if not facts.participant_registry_available or facts.active_champion_count == 0:
        return MVPState.MODEL_PREPARATION_REQUIRED
    if facts.upcoming_event_count == 0:
        return MVPState.UPCOMING_EVENTS_REQUIRED
    if facts.current_quote_count == 0:
        return MVPState.CURRENT_ODDS_REQUIRED
    if facts.placeable_manual_proposal_count > 0:
        return MVPState.PLACEABLE_MANUAL_PROPOSALS_AVAILABLE
    if facts.held_candidate_count > 0:
        return MVPState.ECONOMICALLY_HELD
    return MVPState.CANDIDATES_AVAILABLE


def setup_steps(facts: MVPReadinessFacts) -> tuple[tuple[str, SetupStepState], ...]:
    """Project readiness facts into the fixed first-run checklist."""
    if facts.failure is not None:
        failed = SetupStepState.FAILED
    else:
        failed = SetupStepState.ACTION_REQUIRED
    runtime = SetupStepState.READY if facts.runtime_initialized else SetupStepState.RUNNING
    historical = SetupStepState.READY if facts.historical_snapshot_count else failed
    model = (
        SetupStepState.READY
        if facts.active_champion_count and facts.participant_registry_available
        else failed
    )
    events = SetupStepState.READY if facts.upcoming_event_count else SetupStepState.ACTION_REQUIRED
    odds = SetupStepState.READY if facts.current_quote_count else SetupStepState.ACTION_REQUIRED
    if facts.analysis_running:
        generation = SetupStepState.RUNNING
    elif facts.held_candidate_count and not facts.placeable_manual_proposal_count:
        generation = SetupStepState.OPTIONAL_HELD
    elif facts.placeable_manual_proposal_count or facts.analytical_candidate_count:
        generation = SetupStepState.READY
    else:
        generation = SetupStepState.ACTION_REQUIRED
    return (
        ("Runtime initialized", runtime),
        ("Historical data", historical),
        ("Production model", model),
        ("Upcoming matches", events),
        ("Current odds", odds),
        ("Bet generation", generation),
    )
