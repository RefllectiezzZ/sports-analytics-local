from __future__ import annotations

import json
from pathlib import Path

import pytest

from sports_analytics.mvp.state import (
    MVPReadinessFacts,
    MVPState,
    SetupStepState,
    determine_mvp_state,
    setup_steps,
)


@pytest.mark.parametrize(
    ("facts", "expected"),
    (
        (MVPReadinessFacts(False), MVPState.INITIALIZING),
        (MVPReadinessFacts(True), MVPState.HISTORICAL_DATA_REQUIRED),
        (
            MVPReadinessFacts(True, historical_snapshot_count=1),
            MVPState.MODEL_PREPARATION_REQUIRED,
        ),
        (
            MVPReadinessFacts(
                True,
                historical_snapshot_count=1,
                participant_registry_available=True,
                active_champion_count=1,
            ),
            MVPState.UPCOMING_EVENTS_REQUIRED,
        ),
        (
            MVPReadinessFacts(
                True,
                historical_snapshot_count=1,
                participant_registry_available=True,
                active_champion_count=1,
                upcoming_event_count=1,
            ),
            MVPState.CURRENT_ODDS_REQUIRED,
        ),
        (
            MVPReadinessFacts(
                True,
                historical_snapshot_count=1,
                participant_registry_available=True,
                active_champion_count=1,
                upcoming_event_count=1,
                current_quote_count=3,
                analysis_running=True,
            ),
            MVPState.ANALYSING,
        ),
        (
            MVPReadinessFacts(
                True,
                historical_snapshot_count=1,
                participant_registry_available=True,
                active_champion_count=1,
                upcoming_event_count=1,
                current_quote_count=3,
            ),
            MVPState.CANDIDATES_AVAILABLE,
        ),
        (
            MVPReadinessFacts(
                True,
                historical_snapshot_count=1,
                participant_registry_available=True,
                active_champion_count=1,
                upcoming_event_count=1,
                current_quote_count=3,
                held_candidate_count=1,
            ),
            MVPState.ECONOMICALLY_HELD,
        ),
        (
            MVPReadinessFacts(
                True,
                historical_snapshot_count=1,
                participant_registry_available=True,
                active_champion_count=1,
                upcoming_event_count=1,
                current_quote_count=3,
                placeable_manual_proposal_count=1,
            ),
            MVPState.PLACEABLE_MANUAL_PROPOSALS_AVAILABLE,
        ),
        (
            MVPReadinessFacts(True, failure="sanitized failure"),
            MVPState.FAILED,
        ),
    ),
)
def test_exact_mvp_state_transitions(facts: MVPReadinessFacts, expected: MVPState) -> None:
    assert determine_mvp_state(facts) is expected


def test_vscode_launch_is_package_native_and_portable() -> None:
    launch = json.loads(Path(".vscode/launch.json").read_text(encoding="utf-8"))
    configuration = launch["configurations"][0]

    assert configuration["name"] == "Sports Analytics Local MVP"
    assert configuration["module"] == "sports_analytics.release.cli"
    assert configuration["cwd"] == "${workspaceFolder}"
    encoded = json.dumps(configuration)
    assert "C:\\Users\\" not in encoded
    assert "0.0.0.0" not in encoded


def test_held_generation_step_is_not_presented_as_ready() -> None:
    steps = dict(
        setup_steps(
            MVPReadinessFacts(
                True,
                historical_snapshot_count=1,
                participant_registry_available=True,
                active_champion_count=1,
                upcoming_event_count=1,
                current_quote_count=3,
                analytical_candidate_count=1,
                held_candidate_count=1,
            )
        )
    )

    assert steps["Bet generation"] is SetupStepState.OPTIONAL_HELD
