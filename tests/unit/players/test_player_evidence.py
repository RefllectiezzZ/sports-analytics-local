from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from sports_analytics.core.exceptions import ArtifactError, FeatureError
from sports_analytics.players.evidence import (
    Player,
    PlayerAvailabilityObservation,
    PlayerEvidenceBundle,
    PlayerEvidenceState,
    PlayerEvidenceType,
    PlayerIdentityReconciliation,
    PlayerReconciliationState,
    PlayerRole,
    PlayerTeamMembership,
    SourcePlayer,
    load_player_evidence_artifact,
    parse_player_import_bundle_json,
    parse_player_import_json,
    player_json_template,
    publish_player_evidence_artifact,
)

KICKOFF = datetime(2026, 8, 1, 15, tzinfo=UTC)


def _observation(**overrides):
    values = {
        "canonical_player_id": "player-a",
        "source_player_id": "source-a",
        "canonical_team_id": "team-a",
        "sport_code": "football",
        "source_name": "operator-reviewed",
        "source_observation_id": "observation-a",
        "observed_at_utc": KICKOFF - timedelta(hours=2),
        "event_id": "event-a",
        "effective_date": date(2026, 8, 1),
        "event_start_utc": KICKOFF,
        "status": PlayerEvidenceState.EXPECTED_STARTER,
        "confidence": 0.8,
        "evidence_type": PlayerEvidenceType.EXPECTED_LINEUP,
        "valid_until_utc": KICKOFF,
    }
    values.update(overrides)
    return PlayerAvailabilityObservation(**values)


def test_same_name_players_remain_distinct_and_unresolved_identity_is_explicit() -> None:
    players = (Player("player-a", "football"), Player("player-b", "football"))
    source = (
        SourcePlayer("operator-reviewed", "source-a", "football", "Same Name"),
        SourcePlayer("operator-reviewed", "source-b", "football", "Same Name"),
    )
    unresolved = PlayerIdentityReconciliation(
        "operator-reviewed",
        "source-c",
        None,
        PlayerReconciliationState.UNRESOLVED,
        0.0,
        "player-reconciliation-v1",
        "identity-not-established",
    )
    assert players[0] != players[1]
    assert source[0].display_name == source[1].display_name
    assert unresolved.canonical_player_id is None


def test_membership_chronology_lineup_semantics_minutes_and_staleness() -> None:
    membership = PlayerTeamMembership(
        "player-a",
        "team-a",
        "football",
        date(2026, 7, 1),
        date(2026, 8, 31),
        PlayerRole.ATTACKER,
    )
    observation = _observation()
    assert membership.contains(observation.effective_date)
    assert observation.is_stale(KICKOFF + timedelta(seconds=1))
    assert PlayerEvidenceState.UNKNOWN is not PlayerEvidenceState.AVAILABLE
    with pytest.raises(FeatureError, match="precede event kickoff"):
        _observation(observed_at_utc=KICKOFF)
    with pytest.raises(FeatureError, match="expected minutes"):
        _observation(
            evidence_type=PlayerEvidenceType.EXPECTED_MINUTES,
            expected_minutes=121,
        )


def test_contradictory_confirmed_lineup_rejected() -> None:
    membership = PlayerTeamMembership(
        "player-a",
        "team-a",
        "football",
        date(2026, 1, 1),
        None,
        PlayerRole.ATTACKER,
    )
    first = _observation(
        source_observation_id="confirmed-a",
        evidence_type=PlayerEvidenceType.CONFIRMED_LINEUP,
        status=PlayerEvidenceState.CONFIRMED_STARTER,
    )
    second = _observation(
        source_observation_id="confirmed-b",
        evidence_type=PlayerEvidenceType.CONFIRMED_LINEUP,
        status=PlayerEvidenceState.CONFIRMED_BENCH,
    )
    with pytest.raises(FeatureError, match="contradictory"):
        PlayerEvidenceBundle(
            players=(Player("player-a", "football"),),
            source_players=(),
            reconciliations=(),
            memberships=(membership,),
            observations=(first, second),
        )


def test_current_only_player_artifact_is_display_only_strict_and_tamper_detected(
    tmp_path,
) -> None:
    membership = PlayerTeamMembership(
        "player-a",
        "team-a",
        "football",
        date(2026, 1, 1),
        None,
        PlayerRole.ATTACKER,
    )
    bundle = PlayerEvidenceBundle(
        players=(Player("player-a", "football"),),
        source_players=(SourcePlayer("operator-reviewed", "source-a", "football", "Player A"),),
        reconciliations=(
            PlayerIdentityReconciliation(
                "operator-reviewed",
                "source-a",
                "player-a",
                PlayerReconciliationState.EXACT,
                1.0,
                "player-reconciliation-v1",
            ),
        ),
        memberships=(membership,),
        observations=(_observation(),),
    )
    artifact = publish_player_evidence_artifact(
        root=tmp_path,
        relative_directory="players/current",
        bundle=bundle,
    )
    _, loaded = load_player_evidence_artifact(
        root=tmp_path,
        relative_directory="players/current",
        expected_checksum=artifact.checksum_sha256,
    )
    assert loaded.model_use_state == "display-only-current-context"
    assert parse_player_import_json(player_json_template())[0].canonical_player_id is None
    manifest = tmp_path / "players" / "current" / "manifest.json"
    manifest.write_text(manifest.read_text().replace("player-a", "player-z"), encoding="utf-8")
    with pytest.raises(ArtifactError):
        load_player_evidence_artifact(root=tmp_path, relative_directory="players/current")


def test_operator_import_requires_explicit_reconciliation_and_membership() -> None:
    payload = json.loads(player_json_template())
    row = payload["observations"][0]
    row.update(
        {
            "canonical_player_id": "player-a",
            "source_player_display_name": "Player A",
            "reconciliation_state": "manual",
            "reconciliation_confidence": "1.0",
            "reconciliation_reason": "",
            "membership_valid_from": "2025-07-01",
            "membership_valid_to": "2026-06-30",
            "player_role": "attacker",
        }
    )
    bundle = parse_player_import_bundle_json(json.dumps(payload))
    assert bundle.players == (Player("player-a", "football"),)
    assert bundle.observations[0].canonical_player_id == "player-a"
    assert bundle.memberships[0].contains(date(2026, 1, 1))

    row["membership_valid_from"] = ""
    with pytest.raises(FeatureError, match="membership_valid_from"):
        parse_player_import_bundle_json(json.dumps(payload))
