from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sports_analytics.bookmakers.operator_quotes import (
    FOOTBALL_RULES_SCOPE,
    REGULATION_SCOPE,
    OperatorQuoteInput,
    OperatorQuoteSourceKind,
)
from sports_analytics.models.football_scores import (
    ScoreModelConfiguration,
    ScoreTrainingMatch,
)
from sports_analytics.models.football_tournament import (
    TournamentSplitConfiguration,
    load_tournament_artifact,
)
from sports_analytics.players.evidence import (
    Player,
    PlayerAvailabilityObservation,
    PlayerEvidenceBundle,
    PlayerEvidenceState,
    PlayerEvidenceType,
    PlayerRole,
    PlayerTeamMembership,
)
from sports_analytics.policies.proposal import PublishedProposalPolicy
from sports_analytics.proposals.football import (
    FootballOpportunityPolicy,
    SportCombinationMode,
    load_proposal_artifact,
)
from sports_analytics.services.football_product import (
    FootballProductRequest,
    UpcomingFootballEvent,
    run_and_publish_football_product,
)
from sports_analytics.services.football_product_cli import run_football_product_document
from sports_analytics.ui.product_catalogue import (
    discover_product_read_models,
    load_product_read_model,
)


def test_complete_offline_product_persists_manual_only_read_models(tmp_path) -> None:
    teams = ("a", "b", "c", "d")
    matches = tuple(
        ScoreTrainingMatch(
            canonical_event_id=f"history-{index:03d}",
            competition_id="league",
            event_date=date(2024, 1, 1) + timedelta(days=index),
            home_team_id=teams[index % 4],
            away_team_id=teams[(index + 1) % 4],
            home_goals=(index * 7) % 4,
            away_goals=(index * 5) % 3,
        )
        for index in range(52)
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    upcoming = tuple(
        UpcomingFootballEvent(
            canonical_event_id=f"upcoming-{index}",
            competition_id="league",
            home_team_id="a",
            away_team_id="b",
            event_start_utc=now + timedelta(days=index + 1),
            prediction_cutoff=date(2026, 1, 1),
        )
        for index in range(2)
    )
    quotes = tuple(
        OperatorQuoteInput(
            provider_id="local-book",
            provider_display_name="Local Book",
            sport_code="football",
            canonical_event_id=event.canonical_event_id,
            market_family="match-result",
            outcome_key=outcome,
            line_value=None,
            market_period="full-match",
            participant_scope="event",
            canonical_participant_id=None,
            overtime_scope=REGULATION_SCOPE,
            rules_scope=FOOTBALL_RULES_SCOPE,
            offered_decimal_odds=Decimal(price),
            observed_at_utc=now,
            valid_until_utc=None,
            source_kind=OperatorQuoteSourceKind.MANUAL,
        )
        for event in upcoming
        for outcome, price in (("home", "3.00"), ("draw", "2.00"), ("away", "2.00"))
    )
    published = run_and_publish_football_product(
        exports_root=tmp_path,
        request=FootballProductRequest(
            historical_matches=matches,
            upcoming_events=upcoming,
            operator_quotes=quotes,
            registered_provider_ids=frozenset({"local-book"}),
            evaluated_at_utc=now,
            relative_root="proof",
            score_configuration=ScoreModelConfiguration(minimum_matches=12),
            split_configuration=TournamentSplitConfiguration(
                minimum_training_rows=24,
                calibration_rows=8,
                test_rows=8,
                maximum_folds=2,
            ),
            opportunity_policy=FootballOpportunityPolicy(
                minimum_edge=0.0,
                minimum_expected_value=0.0,
                safety_margin=0.0,
            ),
        ),
    )
    assert published.model_artifact.artifact_id
    assert len(published.probability_artifacts) == 2
    assert any(item.accepted for item in published.proposals.decisions)
    assert published.proposals.accumulators
    assert (
        load_tournament_artifact(
            root=tmp_path,
            relative_directory="proof/tournament",
            expected_checksum=published.tournament_artifact.checksum_sha256,
        ).artifact_id
        == published.tournament_artifact.artifact_id
    )
    assert (
        load_proposal_artifact(
            root=tmp_path,
            relative_directory="proof/proposals",
            expected_checksum=published.proposal_artifact.checksum_sha256,
        ).artifact_id
        == published.proposal_artifact.artifact_id
    )
    entries = discover_product_read_models(tmp_path)
    assert len(entries) == 1
    read_model = load_product_read_model(root=tmp_path, entry=entries[0])
    assert isinstance(read_model.payload, dict)
    assert read_model.payload["product_state"]["placement_state"] == "manual-only"
    assert read_model.payload["product_state"]["automatic_bookmaker_access"] is False
    assert read_model.payload["product_state"]["sport_policy"]["allowed_sports"] == ["football"]
    assert read_model.payload["product_state"]["sport_policy"]["mode"] == "combine-selected-sports"
    assert (
        read_model.payload["model_status"]["production_eligibility_state"]
        == "insufficient-real-evaluation-data"
    )


def test_published_policy_controls_product_sports_limits_and_lineage(tmp_path) -> None:
    policy = PublishedProposalPolicy(
        allowed_sports=("basketball",),
        combination_mode=SportCombinationMode.SEPARATE_BY_SPORT,
        minimum_legs=3,
        maximum_legs=3,
        minimum_total_odds=2.0,
        maximum_total_odds=8.0,
        minimum_edge=0.2,
        minimum_expected_value=0.3,
        maximum_uncertainty=0.04,
    )
    document = {
        "relative_root": "published-policy-proof",
        "evaluated_at_utc": "2026-01-01T00:00:00Z",
        "registered_provider_ids": [],
        "historical_matches": [
            {
                "canonical_event_id": f"history-{index:03d}",
                "competition_id": "league",
                "event_date": (date(2024, 1, 1) + timedelta(days=index)).isoformat(),
                "home_team_id": ("a", "b", "c", "d")[index % 4],
                "away_team_id": ("a", "b", "c", "d")[(index + 1) % 4],
                "home_goals": (index * 7) % 4,
                "away_goals": (index * 5) % 3,
            }
            for index in range(52)
        ],
        "upcoming_events": [
            {
                "canonical_event_id": "upcoming-1",
                "competition_id": "league",
                "home_team_id": "a",
                "away_team_id": "b",
                "event_start_utc": "2026-01-02T00:00:00Z",
                "prediction_cutoff": "2026-01-01",
            }
        ],
        "operator_quotes": [],
        "split_configuration": {
            "minimum_training_rows": 24,
            "calibration_rows": 8,
            "test_rows": 8,
            "maximum_folds": 2,
        },
        "opportunity_policy": {
            "minimum_offered_odds": "1.20",
            "maximum_offered_odds": "20.00",
            "minimum_edge": 0.0,
            "minimum_expected_value": 0.0,
            "safety_margin": 0.0,
        },
    }
    result = run_football_product_document(
        document=document,
        exports_root=tmp_path,
        published_policy=policy,
        published_policy_artifact_id="published-policy-artifact",
    )
    assert result["accepted_single_count"] == 0
    entry = discover_product_read_models(tmp_path)[0]
    read_model = load_product_read_model(root=tmp_path, entry=entry)
    assert (
        read_model.payload["artifact_lineage"]["published_proposal_policy_artifact_id"]
        == "published-policy-artifact"
    )
    assert read_model.payload["product_state"]["sport_policy"] == {
        "allowed_sports": ["basketball"],
        "mode": "separate-by-sport",
        "policy_id": read_model.payload["product_state"]["sport_policy"]["policy_id"],
        "published_configuration_id": policy.configuration_id,
    }
    assert read_model.payload["product_state"]["sport_statuses"] == [
        {"sport_code": "basketball", "status": "sport-model-unavailable"}
    ]
    assert read_model.payload["product_state"]["proposal_limits"]["maximum_uncertainty"] == 0.04


def test_display_only_player_context_cannot_change_future_probability_artifacts(tmp_path) -> None:
    teams = ("a", "b", "c", "d")
    matches = tuple(
        ScoreTrainingMatch(
            canonical_event_id=f"history-{index:03d}",
            competition_id="league",
            event_date=date(2024, 1, 1) + timedelta(days=index),
            home_team_id=teams[index % 4],
            away_team_id=teams[(index + 1) % 4],
            home_goals=(index * 7) % 4,
            away_goals=(index * 5) % 3,
        )
        for index in range(52)
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    upcoming = (
        UpcomingFootballEvent(
            canonical_event_id="upcoming-0",
            competition_id="league",
            home_team_id="a",
            away_team_id="b",
            event_start_utc=now + timedelta(days=1),
            prediction_cutoff=date(2026, 1, 1),
        ),
    )
    common = dict(
        historical_matches=matches,
        upcoming_events=upcoming,
        operator_quotes=(),
        registered_provider_ids=frozenset(),
        evaluated_at_utc=now,
        score_configuration=ScoreModelConfiguration(minimum_matches=12),
        split_configuration=TournamentSplitConfiguration(
            minimum_training_rows=24,
            calibration_rows=8,
            test_rows=8,
            maximum_folds=2,
        ),
        opportunity_policy=FootballOpportunityPolicy(
            minimum_edge=0.0,
            minimum_expected_value=0.0,
            safety_margin=0.0,
        ),
    )
    without_context = run_and_publish_football_product(
        exports_root=tmp_path,
        request=FootballProductRequest(relative_root="without-context", **common),
    )
    player_context = PlayerEvidenceBundle(
        players=(Player("player-a", "football"),),
        source_players=(),
        reconciliations=(),
        memberships=(
            PlayerTeamMembership(
                "player-a", "a", "football", date(2025, 1, 1), None, PlayerRole.ATTACKER
            ),
        ),
        observations=(
            PlayerAvailabilityObservation(
                canonical_player_id="player-a",
                source_player_id="source-a",
                canonical_team_id="a",
                sport_code="football",
                source_name="operator-reviewed",
                source_observation_id="display-only-a",
                observed_at_utc=now,
                event_id="upcoming-0",
                effective_date=date(2026, 1, 2),
                event_start_utc=upcoming[0].event_start_utc,
                status=PlayerEvidenceState.EXPECTED_STARTER,
                confidence=0.8,
                evidence_type=PlayerEvidenceType.EXPECTED_LINEUP,
            ),
        ),
    )
    with_context = run_and_publish_football_product(
        exports_root=tmp_path,
        request=FootballProductRequest(
            relative_root="with-context",
            player_context=player_context,
            player_context_artifact_id="player-context-artifact",
            **common,
        ),
    )
    assert tuple(item.artifact_id for item in with_context.probability_artifacts) == tuple(
        item.artifact_id for item in without_context.probability_artifacts
    )
    assert all(not item.accepted for item in with_context.proposals.decisions)
    assert all(
        "player-train-serve-equivalence-unavailable" in item.reason_codes
        for item in with_context.proposals.decisions
    )
    entry = next(
        item
        for item in discover_product_read_models(tmp_path)
        if item.relative_directory == "with-context/read-model"
    )
    read_model = load_product_read_model(root=tmp_path, entry=entry)
    assert read_model.payload["artifact_lineage"]["player_context_artifact_id"] == (
        "player-context-artifact"
    )
    assert read_model.payload["product_state"]["player_context"]["display_state"] == (
        "display-only-current-context"
    )
    assert len(read_model.payload["product_state"]["player_context"]["observations"]) == 1
