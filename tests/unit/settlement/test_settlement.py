from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sports_analytics.core.exceptions import SettlementError
from sports_analytics.predictions.contracts import CanonicalSelectionIdentity
from sports_analytics.results.contracts import (
    EventResultStatus,
    build_football_full_match_1x2_result,
)
from sports_analytics.results.snapshots import VerifiedResultSnapshot
from sports_analytics.settlement.contracts import (
    SettlementStatus,
    settle_combination,
    settle_single,
)
from sports_analytics.sports.football.markets import match_result_1x2_selection

AS_OF = datetime(2026, 2, 2, 20, tzinfo=UTC)
START = datetime(2026, 2, 2, 18, tzinfo=UTC)


def _snapshot(
    event_id: str,
    *,
    home_score: int = 2,
    away_score: int = 1,
    status: EventResultStatus = EventResultStatus.COMPLETED,
) -> VerifiedResultSnapshot:
    completed = status is EventResultStatus.COMPLETED
    result = build_football_full_match_1x2_result(
        canonical_event_id=event_id,
        scheduled_start_utc=START,
        event_status=status,
        source_name="synthetic-results",
        source_event_id=f"source-{event_id}",
        source_observed_at_utc=AS_OF,
        source_checksum_sha256="a" * 64,
        result_provenance="synthetic-contract",
        home_canonical_participant_id=f"{event_id}-home",
        away_canonical_participant_id=f"{event_id}-away",
        full_time_home_score=home_score if completed else None,
        full_time_away_score=away_score if completed else None,
        result_timestamp_utc=datetime(2026, 2, 2, 19, 55, tzinfo=UTC) if completed else None,
    )
    return VerifiedResultSnapshot(
        snapshot_id=("b" if event_id == "event-1" else "c") * 64,
        checksum_sha256=("d" if event_id == "event-1" else "e") * 64,
        relative_directory=f"results/{event_id}",
        result=result,
    )


def _selection(outcome: str) -> CanonicalSelectionIdentity:
    return CanonicalSelectionIdentity.from_selection(match_result_1x2_selection(outcome))


def _single(
    *,
    event_id: str = "event-1",
    outcome: str = "home",
    snapshot: VerifiedResultSnapshot | None = None,
) -> object:
    return settle_single(
        source_artifact_id="f" * 64,
        source_artifact_checksum_sha256="1" * 64,
        opportunity_id=f"opportunity-{event_id}",
        canonical_event_id=event_id,
        selection=_selection(outcome),
        decimal_odds=Decimal("2.50"),
        result_snapshot=snapshot,
        as_of_utc=AS_OF,
    )


def test_successful_football_single_and_exact_units() -> None:
    settlement = _single(snapshot=_snapshot("event-1"))
    assert settlement.status is SettlementStatus.WIN
    assert settlement.stake_units == Decimal("1")
    assert settlement.returned_units == Decimal("2.50")
    assert settlement.profit_units == Decimal("1.50")
    assert settlement == _single(snapshot=_snapshot("event-1"))


def test_wrong_canonical_event_rejected() -> None:
    with pytest.raises(SettlementError, match="canonical event"):
        _single(event_id="event-2", snapshot=_snapshot("event-1"))


def test_wrong_selection_identity_rejected() -> None:
    synthetic = CanonicalSelectionIdentity(
        sport_code="football",
        market_family="totals",
        market_key="football:totals:full-match:over-under",
        market_period="full-match",
        participant_scope="event",
        canonical_participant_id=None,
        line_type="total",
        line_value=Decimal("2.5"),
        outcome_key="over",
    )
    with pytest.raises(SettlementError, match="selection/result identity"):
        settle_single(
            source_artifact_id="f" * 64,
            source_artifact_checksum_sha256="1" * 64,
            opportunity_id="opportunity-1",
            canonical_event_id="event-1",
            selection=synthetic,
            decimal_odds=Decimal("2"),
            result_snapshot=_snapshot("event-1"),
            as_of_utc=AS_OF,
        )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (EventResultStatus.POSTPONED, SettlementStatus.PENDING),
        (EventResultStatus.CANCELLED, SettlementStatus.VOID),
        (EventResultStatus.ABANDONED, SettlementStatus.VOID),
        (EventResultStatus.INCOMPLETE, SettlementStatus.UNRESOLVED),
    ],
)
def test_non_completed_result_statuses(
    status: EventResultStatus,
    expected: SettlementStatus,
) -> None:
    assert _single(snapshot=_snapshot("event-1", status=status)).status is expected


def test_combination_win_loss_unresolved_and_void_policy() -> None:
    winning_legs = (
        ("op-1", "event-1", _selection("home"), Decimal("2"), _snapshot("event-1")),
        ("op-2", "event-2", _selection("home"), Decimal("3"), _snapshot("event-2")),
    )
    won = settle_combination(
        source_artifact_id="f" * 64,
        source_artifact_checksum_sha256="1" * 64,
        combination_id="combo",
        legs=winning_legs,
        persisted_decimal_odds=Decimal("6"),
        as_of_utc=AS_OF,
    )
    assert (won.status, won.returned_units, won.profit_units) == (
        SettlementStatus.WIN,
        Decimal("6"),
        Decimal("5"),
    )
    for combination_id, legs, expected in (
        (
            "combo-loss",
            (
                winning_legs[0],
                (
                    "op-2",
                    "event-2",
                    _selection("away"),
                    Decimal("3"),
                    _snapshot("event-2"),
                ),
            ),
            SettlementStatus.LOSS,
        ),
        (
            "combo-unresolved",
            (
                winning_legs[0],
                (
                    "op-2",
                    "event-2",
                    _selection("home"),
                    Decimal("3"),
                    _snapshot("event-2", status=EventResultStatus.INCOMPLETE),
                ),
            ),
            SettlementStatus.UNRESOLVED,
        ),
    ):
        assert (
            settle_combination(
                source_artifact_id="f" * 64,
                source_artifact_checksum_sha256="1" * 64,
                combination_id=combination_id,
                legs=legs,
                persisted_decimal_odds=Decimal("6"),
                as_of_utc=AS_OF,
            ).status
            is expected
        )
    void_legs = (
        winning_legs[0],
        (
            "op-2",
            "event-2",
            _selection("home"),
            Decimal("3"),
            _snapshot("event-2", status=EventResultStatus.CANCELLED),
        ),
    )
    settled = settle_combination(
        source_artifact_id="f" * 64,
        source_artifact_checksum_sha256="1" * 64,
        combination_id="combo-void",
        legs=void_legs,
        persisted_decimal_odds=Decimal("6"),
        as_of_utc=AS_OF,
    )
    assert settled.status is SettlementStatus.WIN
    assert settled.returned_units == Decimal("2")
    assert settled.warnings == ("void-or-push-legs-retained-at-unit-return",)
