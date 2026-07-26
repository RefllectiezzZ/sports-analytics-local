from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from sports_analytics.core.exceptions import ResultError
from sports_analytics.data.codec import dumps_canonical_json
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.predictions.contracts import CanonicalSelectionIdentity
from sports_analytics.results.contracts import (
    RESULT_IDENTITY_VERSION,
    EventResultStatus,
    MarketOutcome,
    ParticipantResult,
    build_canonical_result,
    build_football_full_match_1x2_result,
)
from sports_analytics.results.snapshots import (
    RESULT_SNAPSHOT_ARTIFACT_TYPE,
    RESULT_SNAPSHOT_SCHEMA_VERSION,
    load_result_snapshot,
    publish_result_snapshot,
)

AS_OF = datetime(2026, 1, 2, 20, tzinfo=UTC)
START = datetime(2026, 1, 2, 18, tzinfo=UTC)


def _result(**overrides: object) -> object:
    values = {
        "canonical_event_id": "event-1",
        "scheduled_start_utc": START,
        "event_status": EventResultStatus.COMPLETED,
        "source_name": "synthetic-results",
        "source_event_id": "source-event-1",
        "source_observed_at_utc": AS_OF,
        "source_checksum_sha256": "a" * 64,
        "result_provenance": "synthetic-contract-fixture",
        "home_canonical_participant_id": "participant-home",
        "away_canonical_participant_id": "participant-away",
        "full_time_home_score": 2,
        "full_time_away_score": 1,
        "result_timestamp_utc": datetime(2026, 1, 2, 19, 55, tzinfo=UTC),
    }
    values.update(overrides)
    return build_football_full_match_1x2_result(**values)  # type: ignore[arg-type]


def test_canonical_result_identity_is_deterministic() -> None:
    first = _result()
    second = _result()
    assert first == second
    assert first.canonical_result_id == second.canonical_result_id
    assert first.identity_version == RESULT_IDENTITY_VERSION


def test_generic_multi_sport_multi_outcome_contract_is_not_team_or_provider_specific() -> None:
    selections = tuple(
        CanonicalSelectionIdentity(
            sport_code="tennis",
            market_family="match-result",
            market_key="tennis.match-result.winner.full-match",
            market_period="full-match",
            participant_scope="event",
            canonical_participant_id=None,
            line_type="none",
            line_value=None,
            outcome_key=outcome,
        )
        for outcome in ("participant-a", "participant-b")
    )
    result = build_canonical_result(
        canonical_event_id="synthetic-tennis-event",
        sport_code="tennis",
        event_status=EventResultStatus.COMPLETED,
        scheduled_start_utc=START,
        result_timestamp_utc=AS_OF,
        source_name="synthetic-provider-independent",
        source_event_id="opaque-source-event",
        source_observed_at_utc=AS_OF,
        result_provenance="generic-contract-test",
        participant_results=(
            ParticipantResult("participant-a", "participant-1", 2),
            ParticipantResult("participant-b", "participant-2", 0),
        ),
        market_outcomes=(
            MarketOutcome(selections[0], "win"),
            MarketOutcome(selections[1], "loss"),
        ),
        source_checksum_sha256="f" * 64,
    )
    assert result.sport_code == "tennis"
    assert result.outcome_for(selections[0]) == "win"


@pytest.mark.parametrize("status", ["finished", "unknown", "free text"])
def test_invalid_event_status_rejected(status: str) -> None:
    with pytest.raises(ResultError, match="unsupported event result status"):
        _result(event_status=status)


@pytest.mark.parametrize(
    ("home", "away"),
    [(None, 1), (1, None), (-1, 0), (1.5, 0), (True, 0)],
)
def test_invalid_partial_or_non_integer_football_score_rejected(
    home: object,
    away: object,
) -> None:
    with pytest.raises(ResultError, match="score"):
        _result(full_time_home_score=home, full_time_away_score=away)


def test_contradictory_claimed_outcome_rejected() -> None:
    with pytest.raises(ResultError, match="contradicts"):
        _result(claimed_outcome_key="away")


@pytest.mark.parametrize(
    "status",
    [
        EventResultStatus.POSTPONED,
        EventResultStatus.CANCELLED,
        EventResultStatus.ABANDONED,
        EventResultStatus.INCOMPLETE,
    ],
)
def test_non_completed_status_has_no_trusted_outcome(status: EventResultStatus) -> None:
    result = _result(
        event_status=status,
        full_time_home_score=None,
        full_time_away_score=None,
        result_timestamp_utc=None,
    )
    assert result.market_outcomes == ()
    assert result.participant_results == ()


def test_result_snapshot_publication_and_tamper_rejection(tmp_path) -> None:
    result = _result()
    published = publish_result_snapshot(
        root=tmp_path,
        relative_directory="results/event-1",
        result=result,
    )
    replay = load_result_snapshot(
        root=tmp_path,
        relative_directory="results/event-1",
        expected_checksum=published.checksum_sha256,
        expected_snapshot_id=published.snapshot_id,
    )
    assert replay == published
    assert (
        publish_result_snapshot(
            root=tmp_path,
            relative_directory="results/event-1",
            result=result,
        )
        == published
    )

    manifest_path = tmp_path / "results" / "event-1" / "manifest.json"
    sidecar_path = tmp_path / "results" / "event-1" / "manifest_checksum.sha256"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["payload"]["result"]["source_event_id"] = "forged-source-event"
    document["artifact_id"] = content_addressed_id(
        identity_type=(
            f"analytical-artifact:{RESULT_SNAPSHOT_ARTIFACT_TYPE}:{RESULT_SNAPSHOT_SCHEMA_VERSION}"
        ),
        payload={"payload": document["payload"]},
    )
    raw = (dumps_canonical_json(document) + "\n").encode()
    manifest_path.write_bytes(raw)
    sidecar_path.write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="utf-8")
    with pytest.raises(ResultError, match="forged|identity"):
        load_result_snapshot(root=tmp_path, relative_directory="results/event-1")


def test_result_snapshot_rejects_path_traversal(tmp_path) -> None:
    with pytest.raises(ResultError):
        load_result_snapshot(root=tmp_path, relative_directory="../result")
