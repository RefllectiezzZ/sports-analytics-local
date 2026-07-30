from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from sports_analytics.bookmakers.operator_quotes import (
    FOOTBALL_RULES_SCOPE,
    REGULATION_SCOPE,
    OperatorQuoteInput,
    OperatorQuoteSourceKind,
)
from sports_analytics.core.exceptions import (
    ArtifactError,
    ConfigurationError,
    EvaluationError,
    GovernanceError,
    ValueEvaluationError,
)
from sports_analytics.data.codec import dumps_canonical_json, format_utc_timestamp
from sports_analytics.models.football_scores import (
    FOOTBALL_SCORE_MODEL_VERSION,
    FootballScoreModel,
    ScoreModelConfiguration,
    ScoreModelDiagnostics,
    write_score_model_artifact,
)
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
    publish_player_evidence_artifact,
)
from sports_analytics.policies.proposal import PublishedProposalPolicy, publish_proposal_policy
from sports_analytics.services import football_product as research_product
from sports_analytics.services.champion_resolution import (
    resolve_active_score_champion,
    write_score_calibration_artifact,
)
from sports_analytics.services.production_football_product import (
    ProductionFootballProductRequest,
    run_and_publish_production_football_product,
)
from sports_analytics.services.production_football_product_cli import (
    run_production_football_product_document,
)
from sports_analytics.sports.football.participant_registry import (
    RegisteredFootballParticipant,
    load_participant_registry_artifact,
    write_participant_registry_artifact,
)
from sports_analytics.ui.product_catalogue import ProductReadModelEntry, load_product_read_model
from sports_analytics.upcoming_events import (
    parse_upcoming_event_json,
    upcoming_event_json_template,
    write_upcoming_event_artifact,
)

NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)
MARKET_KEY = "football.score.full-match"


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE model_registry_entries (
          model_artifact_id TEXT PRIMARY KEY,
          model_checksum_sha256 TEXT NOT NULL,
          model_relative_path TEXT NOT NULL,
          model_specification_version TEXT NOT NULL,
          feature_specification_version TEXT NOT NULL,
          sport_code TEXT NOT NULL,
          market_key TEXT NOT NULL,
          role TEXT NOT NULL,
          lifecycle_status TEXT NOT NULL,
          registered_at TEXT NOT NULL,
          actor TEXT NOT NULL,
          provenance_json TEXT NOT NULL,
          superseded_model_artifact_id TEXT,
          version INTEGER NOT NULL
        )
        """
    )
    return connection


def _evidence(tmp_path):
    exports = tmp_path / "exports"
    models = tmp_path / "models"
    events = parse_upcoming_event_json(
        upcoming_event_json_template().encode(), evaluated_at_utc=NOW
    )
    registry_rows = tuple(
        RegisteredFootballParticipant(
            participant_id,
            "football",
            "team",
            f"Team {index}",
            ("prt-primeira-liga",),
            "exact",
            "verified-football-snapshot",
            f"source-team-{index}",
            "verified-source-artifact",
            "0" * 64,
            date(2026, 7, 1),
            None,
        )
        for index, participant_id in enumerate(
            sorted(
                {
                    events[0].canonical_home_participant_id,
                    events[0].canonical_away_participant_id,
                }
            )
        )
    )
    registry_artifact = write_participant_registry_artifact(
        root=exports,
        relative_directory="evidence/participants",
        registry_revision="registry-1",
        generated_at_utc=NOW,
        evaluated_at_utc=NOW,
        participants=registry_rows,
    )
    registry = load_participant_registry_artifact(
        root=exports,
        relative_directory="evidence/participants",
        expected_artifact_id=registry_artifact.artifact_id,
        expected_checksum=registry_artifact.checksum_sha256,
    )
    event_artifact = write_upcoming_event_artifact(
        root=exports,
        relative_directory="evidence/events",
        events=events,
        evaluated_at_utc=NOW,
        participant_registry=registry,
    )
    policy_artifact = publish_proposal_policy(
        root=exports,
        relative_directory="evidence/policy",
        policy=PublishedProposalPolicy(allowed_sports=("football",)),
    )
    request = ProductionFootballProductRequest(
        upcoming_event_relative_directory="evidence/events",
        upcoming_event_artifact_id=event_artifact.artifact_id,
        upcoming_event_checksum_sha256=event_artifact.checksum_sha256,
        participant_registry_relative_directory="evidence/participants",
        participant_registry_artifact_id=registry_artifact.artifact_id,
        participant_registry_checksum_sha256=registry_artifact.checksum_sha256,
        competition_id="prt-primeira-liga",
        market_key=MARKET_KEY,
        evaluated_at_utc=NOW,
        relative_root="product",
        proposal_policy_relative_directory="evidence/policy",
        proposal_policy_checksum_sha256=policy_artifact.checksum_sha256,
    )
    return exports, models, events, request


def _register_champion(connection, models, events, *, model_teams=None):
    teams = model_teams or tuple(
        sorted(
            {
                events[0].canonical_home_participant_id,
                events[0].canonical_away_participant_id,
            }
        )
    )
    model = FootballScoreModel(
        model_family="independent-poisson",
        competition_id="prt-primeira-liga",
        training_start=date(2024, 1, 1),
        training_end=date(2026, 7, 1),
        teams=teams,
        base_log_rate=0.1,
        home_advantage=0.15,
        attack_strengths=tuple(0.0 for _ in teams),
        defence_strengths=tuple(0.0 for _ in teams),
        rho=0.0,
        configuration=ScoreModelConfiguration(minimum_matches=1),
        diagnostics=ScoreModelDiagnostics(True, 1, 1.0, 0.0, 0),
    )
    artifact = write_score_model_artifact(root=models, relative_directory="champion", model=model)
    calibration = write_score_calibration_artifact(
        root=models,
        relative_directory="champion-calibration",
        model_artifact_id=artifact.artifact_id,
        training_lineage="training-artifact",
        temperature=1.0,
    )
    provenance = {
        "competition_id": "prt-primeira-liga",
        "model_purpose": "football-fair-odds",
        "probability_generator_scope": "football-score-surface-full-match",
        "evaluation_mode": "prospective-operator",
        "artifact_type": "football-score-model",
        "artifact_schema": "football-score-model-v1",
        "model_family": "independent-poisson",
        "training_lineage": "training-artifact",
        "calibration": {
            "method": "global-temperature",
            "relative_directory": "champion-calibration",
            "lineage_artifact_id": calibration.artifact_id,
            "lineage_checksum_sha256": calibration.checksum_sha256,
        },
    }
    connection.execute(
        """
        INSERT INTO model_registry_entries VALUES
        (?, ?, ?, ?, ?, ?, ?, 'champion', 'promoted', ?, 'operator', ?, NULL, 3)
        """,
        (
            artifact.artifact_id,
            artifact.checksum_sha256,
            "champion",
            FOOTBALL_SCORE_MODEL_VERSION,
            "score-history-v1",
            "football",
            MARKET_KEY,
            format_utc_timestamp(NOW),
            dumps_canonical_json(provenance),
        ),
    )
    return artifact


def _quotes(event_id: str) -> tuple[OperatorQuoteInput, ...]:
    return tuple(
        OperatorQuoteInput(
            provider_id="operator-book",
            provider_display_name="Operator Book",
            sport_code="football",
            canonical_event_id=event_id,
            market_family="match-result",
            outcome_key=outcome,
            line_value=None,
            market_period="full-match",
            participant_scope="event",
            canonical_participant_id=None,
            overtime_scope=REGULATION_SCOPE,
            rules_scope=FOOTBALL_RULES_SCOPE,
            offered_decimal_odds=Decimal(odds),
            observed_at_utc=NOW,
            valid_until_utc=datetime(2026, 8, 1, 12, 10, tzinfo=UTC),
            source_kind=OperatorQuoteSourceKind.MANUAL,
            import_batch_id="quote-batch-1",
        )
        for outcome, odds in (("home", "2.20"), ("draw", "3.50"), ("away", "3.60"))
    )


def _player_artifact(exports, event, *, event_id: str | None = None):
    team_id = event.canonical_home_participant_id
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
        memberships=(
            PlayerTeamMembership(
                "player-a",
                team_id,
                "football",
                date(2026, 7, 1),
                None,
                PlayerRole.ATTACKER,
            ),
        ),
        observations=(
            PlayerAvailabilityObservation(
                canonical_player_id="player-a",
                source_player_id="source-a",
                canonical_team_id=team_id,
                sport_code="football",
                source_name="operator-reviewed",
                source_observation_id=f"observation-{event_id or 'verified'}",
                observed_at_utc=NOW,
                event_id=event_id or event.canonical_event_id,
                effective_date=date(2026, 8, 15),
                event_start_utc=event.event_start_utc,
                status=PlayerEvidenceState.EXPECTED_STARTER,
                confidence=0.8,
                evidence_type=PlayerEvidenceType.EXPECTED_LINEUP,
                valid_until_utc=event.event_start_utc,
            ),
        ),
    )
    return publish_player_evidence_artifact(
        root=exports,
        relative_directory=f"evidence/player-{event_id or 'verified'}",
        bundle=bundle,
    )


def test_no_champion_publishes_truthful_unavailable_state(tmp_path) -> None:
    exports, models, events, request = _evidence(tmp_path)
    # A valid newest-looking file on disk is deliberately not a registry fallback.
    _register_champion(_connection(), models, events)
    result = run_and_publish_production_football_product(
        connection=_connection(),
        exports_root=exports,
        model_root=models,
        request=request,
    )
    assert not result.probability_artifacts
    assert result.proposals is None
    entry = ProductReadModelEntry(
        "product/read-model",
        result.read_model_artifact.artifact_id,
        result.read_model_artifact.checksum_sha256,
    )
    payload = load_product_read_model(root=exports, entry=entry).payload
    assert isinstance(payload, dict)
    assert payload["model_status"]["state"] == "no-production-champion"
    assert payload["product_state"]["eligibility"] == {
        "model_artifact_valid": False,
        "fair_odds_eligible": False,
        "opportunity_analysis_eligible": False,
        "bet_proposal_eligible": False,
        "promotion_eligible": False,
    }


def test_production_uses_registered_champion_and_never_trains(tmp_path, monkeypatch) -> None:
    exports, models, events, request = _evidence(tmp_path)
    connection = _connection()
    artifact = _register_champion(connection, models, events)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("production invoked training")

    for name in (
        "run_score_tournament",
        "run_unified_tournament",
        "fit_independent_poisson",
        "fit_dixon_coles",
    ):
        monkeypatch.setattr(research_product, name, forbidden)
    result = run_and_publish_production_football_product(
        connection=connection,
        exports_root=exports,
        model_root=models,
        request=request,
    )
    assert result.probability_artifacts
    assert result.proposals is None
    assert result.proposal_artifact is None
    assert result.read_model_artifact.payload["model_status"]["model_artifact_id"] == (
        artifact.artifact_id
    )
    assert result.read_model_artifact.payload["product_state"]["operational_state"] == (
        "fair-odds-only"
    )
    identity = result.read_model_artifact.payload["model_status"]["participant_identity_by_event"][
        events[0].canonical_event_id
    ]
    assert identity["home_participant_identity_state"] == "registered-model-seen"
    assert identity["unseen_team_fallback_used"] is False


def test_registered_model_unseen_team_uses_recorded_competition_average_fallback(
    tmp_path,
) -> None:
    exports, models, events, request = _evidence(tmp_path)
    connection = _connection()
    _register_champion(
        connection,
        models,
        events,
        model_teams=(events[0].canonical_home_participant_id,),
    )
    result = run_and_publish_production_football_product(
        connection=connection,
        exports_root=exports,
        model_root=models,
        request=request,
    )
    identity = result.read_model_artifact.payload["model_status"]["participant_identity_by_event"][
        events[0].canonical_event_id
    ]
    assert identity["away_participant_identity_state"] == "registered-model-unseen"
    assert identity["unseen_team_fallback_used"] is True
    assert identity["unseen_participant_ids"] == [events[0].canonical_away_participant_id]
    assert identity["fallback_policy"] == "competition-average-zero-effect"
    assert result.probability_artifacts[0].payload["participant_identity"] == identity


def test_product_rejects_mismatched_participant_registry_before_probability(tmp_path) -> None:
    exports, models, events, request = _evidence(tmp_path)
    connection = _connection()
    _register_champion(connection, models, events)
    original = load_participant_registry_artifact(
        root=exports,
        relative_directory=request.participant_registry_relative_directory,
        expected_artifact_id=request.participant_registry_artifact_id,
        expected_checksum=request.participant_registry_checksum_sha256,
    )
    duplicate = write_participant_registry_artifact(
        root=exports,
        relative_directory="evidence/participants-copy",
        registry_revision=original.registry_revision,
        generated_at_utc=original.generated_at_utc,
        evaluated_at_utc=original.evaluated_at_utc,
        participants=original.participants,
    )
    with pytest.raises(ArtifactError, match="participant registry lineage mismatch"):
        run_and_publish_production_football_product(
            connection=connection,
            exports_root=exports,
            model_root=models,
            request=replace(
                request,
                participant_registry_relative_directory="evidence/participants-copy",
                participant_registry_artifact_id=duplicate.artifact_id,
                participant_registry_checksum_sha256=duplicate.checksum_sha256,
            ),
        )
    assert not (exports / "product" / "probabilities").exists()


def test_current_quote_is_analysed_but_economic_holds_prevent_proposal(tmp_path) -> None:
    exports, models, events, request = _evidence(tmp_path)
    connection = _connection()
    _register_champion(connection, models, events)
    player = _player_artifact(exports, events[0])
    result = run_and_publish_production_football_product(
        connection=connection,
        exports_root=exports,
        model_root=models,
        request=replace(
            request,
            operator_quotes=_quotes(events[0].canonical_event_id),
            registered_provider_ids=frozenset({"operator-book"}),
            player_context_relative_directory=player.relative_directory,
            player_context_checksum_sha256=player.checksum_sha256,
        ),
    )
    assert result.quote_artifact is not None
    assert result.proposal_artifact is not None
    assert result.proposals is not None
    analysed = tuple(
        item for item in result.proposals.decisions if item.offered_decimal_odds is not None
    )
    assert len(analysed) == 3
    assert all(item.edge is not None and item.expected_value is not None for item in analysed)
    assert all(not item.accepted for item in analysed)
    assert all("no-prospective-settlement-cycle" in item.reason_codes for item in analysed)
    state = result.read_model_artifact.payload["product_state"]
    assert state["operational_state"] == "economic-evidence-hold"
    assert state["eligibility"]["opportunity_analysis_eligible"] is True
    assert state["eligibility"]["bet_proposal_eligible"] is False
    assert state["analytical_candidate_count"] == 3
    assert state["placeable_manual_proposal_count"] == 0
    loaded = load_product_read_model(
        root=exports,
        entry=ProductReadModelEntry(
            "product/read-model",
            result.read_model_artifact.artifact_id,
            result.read_model_artifact.checksum_sha256,
        ),
    )
    assert loaded.payload["product_state"]["sport_policy"]["allowed_sports"] == ["football"]
    assert loaded.payload["product_state"]["player_context"]["display_state"] == (
        "display-only-current-context"
    )


def test_quote_must_reference_verified_upcoming_event(tmp_path) -> None:
    exports, models, events, request = _evidence(tmp_path)
    connection = _connection()
    _register_champion(connection, models, events)
    bad = tuple(replace(item, canonical_event_id="unverified-event") for item in _quotes("unused"))
    with pytest.raises(ValueEvaluationError, match="absent or unresolved"):
        run_and_publish_production_football_product(
            connection=connection,
            exports_root=exports,
            model_root=models,
            request=replace(
                request,
                operator_quotes=bad,
                registered_provider_ids=frozenset({"operator-book"}),
            ),
        )


def test_player_context_is_reconciled_and_probability_identity_is_invariant(tmp_path) -> None:
    exports, models, events, request = _evidence(tmp_path)
    connection = _connection()
    _register_champion(connection, models, events)
    without_context = run_and_publish_production_football_product(
        connection=connection,
        exports_root=exports,
        model_root=models,
        request=replace(request, relative_root="product/no-player"),
    )
    player = _player_artifact(exports, events[0])
    with_context = run_and_publish_production_football_product(
        connection=connection,
        exports_root=exports,
        model_root=models,
        request=replace(
            request,
            relative_root="product/with-player",
            player_context_relative_directory=player.relative_directory,
            player_context_checksum_sha256=player.checksum_sha256,
        ),
    )
    assert tuple(item.artifact_id for item in without_context.probability_artifacts) == tuple(
        item.artifact_id for item in with_context.probability_artifacts
    )
    assert (
        with_context.read_model_artifact.payload["product_state"]["player_context"][
            "model_use_state"
        ]
        == "player-context-not-trainable"
    )
    assert (
        "player_context_artifact_id" in with_context.read_model_artifact.payload["artifact_lineage"]
    )
    assert (
        with_context.read_model_artifact.payload["model_status"]["player_context_consumption"]
        == "not-consumed"
    )


def test_unverified_player_event_is_rejected(tmp_path) -> None:
    exports, models, events, request = _evidence(tmp_path)
    connection = _connection()
    _register_champion(connection, models, events)
    player = _player_artifact(exports, events[0], event_id="unverified-event")
    with pytest.raises(EvaluationError, match="unverified upcoming event"):
        run_and_publish_production_football_product(
            connection=connection,
            exports_root=exports,
            model_root=models,
            request=replace(
                request,
                player_context_relative_directory=player.relative_directory,
                player_context_checksum_sha256=player.checksum_sha256,
            ),
        )


def test_champion_checksum_competition_and_multiplicity_are_fail_closed(tmp_path) -> None:
    exports, models, events, request = _evidence(tmp_path)
    connection = _connection()
    artifact = _register_champion(connection, models, events)
    connection.execute(
        "UPDATE model_registry_entries SET model_checksum_sha256 = ?",
        ("b" * 64,),
    )
    with pytest.raises(ArtifactError, match="checksum"):
        run_and_publish_production_football_product(
            connection=connection,
            exports_root=exports,
            model_root=models,
            request=request,
        )
    connection.execute(
        "UPDATE model_registry_entries SET model_checksum_sha256 = ?",
        (artifact.checksum_sha256,),
    )
    with pytest.raises(EvaluationError, match="competition"):
        run_and_publish_production_football_product(
            connection=connection,
            exports_root=exports,
            model_root=models,
            request=replace(request, competition_id="eng-premier-league"),
        )
    connection.execute(
        """
        INSERT INTO model_registry_entries
        SELECT ?, model_checksum_sha256, model_relative_path,
               model_specification_version, feature_specification_version,
               sport_code, market_key, role, lifecycle_status, registered_at,
               actor, provenance_json, superseded_model_artifact_id, version
        FROM model_registry_entries
        """,
        ("0" * 64,),
    )
    with pytest.raises(GovernanceError, match="multiple"):
        run_and_publish_production_football_product(
            connection=connection,
            exports_root=exports,
            model_root=models,
            request=request,
        )


def test_inference_revalidates_current_event_cutoff_before_writing_probabilities(tmp_path) -> None:
    exports, models, events, request = _evidence(tmp_path)
    connection = _connection()
    _register_champion(connection, models, events)
    with pytest.raises(EvaluationError, match="event-no-longer-pre-match"):
        run_and_publish_production_football_product(
            connection=connection,
            exports_root=exports,
            model_root=models,
            request=replace(request, evaluated_at_utc=events[0].event_start_utc),
        )
    assert not (exports / "product" / "probabilities").exists()
    valid = run_and_publish_production_football_product(
        connection=connection,
        exports_root=exports,
        model_root=models,
        request=replace(
            request,
            relative_root="product/one-microsecond-before",
            evaluated_at_utc=events[0].event_start_utc - timedelta(microseconds=1),
        ),
    )
    assert valid.probability_artifacts


def test_champions_in_distinct_competitions_coexist_in_one_registry(tmp_path) -> None:
    exports, models, events, _request = _evidence(tmp_path)
    connection = _connection()
    champion = _register_champion(connection, models, events)
    portugal = connection.execute(
        "SELECT provenance_json FROM model_registry_entries WHERE model_artifact_id = ?",
        (champion.artifact_id,),
    ).fetchone()["provenance_json"]
    england = json.loads(portugal)
    england["competition_id"] = "eng-premier-league"
    connection.execute(
        """
        INSERT INTO model_registry_entries
        SELECT ?, model_checksum_sha256, model_relative_path,
               model_specification_version, feature_specification_version,
               sport_code, market_key, role, lifecycle_status, registered_at,
               actor, ?, superseded_model_artifact_id, version
        FROM model_registry_entries
        """,
        ("england-champion", dumps_canonical_json(england)),
    )
    resolved = resolve_active_score_champion(
        connection=connection,
        model_root=models,
        competition_id="prt-primeira-liga",
        market_key=MARKET_KEY,
    )
    assert resolved is not None
    assert resolved.model_artifact_id == champion.artifact_id


def test_production_json_rejects_inline_training_and_model_fields(tmp_path) -> None:
    exports, models, _events, request = _evidence(tmp_path)
    base = {
        "relative_root": request.relative_root,
        "evaluated_at_utc": format_utc_timestamp(NOW),
        "competition_id": request.competition_id,
        "market_key": request.market_key,
        "upcoming_event_artifact": {
            "relative_directory": request.upcoming_event_relative_directory,
            "artifact_id": request.upcoming_event_artifact_id,
            "checksum_sha256": request.upcoming_event_checksum_sha256,
        },
        "participant_registry_artifact": {
            "relative_directory": request.participant_registry_relative_directory,
            "artifact_id": request.participant_registry_artifact_id,
            "checksum_sha256": request.participant_registry_checksum_sha256,
        },
        "proposal_policy_artifact": {
            "relative_directory": request.proposal_policy_relative_directory,
            "checksum_sha256": request.proposal_policy_checksum_sha256,
        },
        "registered_provider_ids": [],
        "operator_quotes": [],
        "player_context_artifact": None,
    }
    for forbidden in ("historical_matches", "split_configuration", "model"):
        with pytest.raises(ConfigurationError, match="fields are not exact"):
            run_production_football_product_document(
                document={**base, forbidden: []},
                connection=_connection(),
                exports_root=exports,
                model_root=models,
            )
