from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from sports_analytics.artifacts import write_analytical_artifact
from sports_analytics.core.paths import RuntimePaths, create_runtime_directories
from sports_analytics.data.database import connect_database
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.governance.contracts import ModelEvaluationEvidence
from sports_analytics.models.football_evaluation import EvaluationProvenance
from sports_analytics.models.football_scores import (
    FootballScoreModel,
    ScoreModelConfiguration,
    ScoreModelDiagnostics,
    ScoreTrainingMatch,
    write_score_model_artifact,
)
from sports_analytics.models.football_tournament import (
    FootballScoreTournament,
    TournamentCandidateSummary,
    TournamentFoldMetric,
    build_tournament_folds,
    write_tournament_artifact,
)
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.mvp import champion_preparation as preparation
from sports_analytics.mvp.orchestrator import MVPOrchestrator
from sports_analytics.mvp.state import MVPState
from sports_analytics.services.champion_resolution import (
    write_score_calibration_artifact,
)
from sports_analytics.sports.football.markets import MARKET_KEY_MATCH_RESULT_1X2
from sports_analytics.sports.football.participant_registry import (
    PARTICIPANT_SOURCE_ROLE,
    ParticipantSourceReference,
)

NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


def _paths(tmp_path: Path) -> RuntimePaths:
    storage = tmp_path / "runtime"
    paths = RuntimePaths(
        base_directory=tmp_path,
        storage_root=storage,
        sqlite_path=storage / "operational.sqlite3",
        raw_directory=storage / "raw",
        snapshots_directory=storage / "snapshots",
        features_directory=storage / "features",
        models_directory=storage / "models",
        exports_directory=storage / "exports",
        logs_directory=storage / "logs",
    )
    create_runtime_directories(paths)
    ensure_database_ready(paths.sqlite_path)
    return paths


def _tournament(
    *,
    provenance: EvaluationProvenance = EvaluationProvenance.VERIFIED_HISTORICAL,
    eligibility: str = "production-eligible",
    reasons: tuple[str, ...] = (),
) -> FootballScoreTournament:
    return FootballScoreTournament(
        candidates=preparation._CANDIDATES,
        folds=(),
        fold_metrics=(),
        summaries=(),
        provisional_winner_candidate_id=preparation._CANDIDATES[1].candidate_id,
        provisional_winner_reason="verified fixture boundary",
        evaluation_provenance=provenance,
        production_eligibility_state=eligibility,
        production_ineligibility_reasons=reasons,
        multinomial_baseline_state="retained",
        market_baseline_state="unavailable",
    )


def _history() -> preparation._CompetitionHistory:
    return preparation._CompetitionHistory(
        competition_id="eng-premier-league",
        references=(),
        matches=(),
    )


def _persistable_history() -> preparation._CompetitionHistory:
    start = date(2023, 1, 1)
    return preparation._CompetitionHistory(
        competition_id="eng-premier-league",
        references=(),
        matches=tuple(
            ScoreTrainingMatch(
                canonical_event_id=f"event-{index:04d}",
                competition_id="eng-premier-league",
                event_date=start + timedelta(days=index),
                home_team_id=f"team-{index % 20:02d}",
                away_team_id=f"team-{(index + 1) % 20:02d}",
                home_goals=index % 4,
                away_goals=(index + 1) % 3,
            )
            for index in range(700)
        ),
    )


def _persistable_tournament(
    history: preparation._CompetitionHistory,
) -> FootballScoreTournament:
    folds = build_tournament_folds(history.matches, configuration=preparation._SPLIT)
    metrics = tuple(
        TournamentFoldMetric(
            candidate_id=candidate.candidate_id,
            fold_id=fold.fold_id,
            training_end=max(item.event_date for item in fold.training),
            calibration_start=min(item.event_date for item in fold.calibration),
            calibration_end=max(item.event_date for item in fold.calibration),
            test_start=min(item.event_date for item in fold.test),
            test_end=max(item.event_date for item in fold.test),
            temperature=1.0,
            exact_score_negative_log_likelihood=0.8 - (index * 0.1),
            result_log_loss=0.7 - (index * 0.1),
            result_brier=0.5 - (index * 0.1),
            ranked_probability_score=0.4 - (index * 0.1),
            mean_absolute_goal_error=1.2 - (index * 0.1),
            test_rows=len(fold.test),
            converged=True,
        )
        for index, candidate in enumerate(preparation._CANDIDATES)
        for fold in folds
    )
    summaries = tuple(
        TournamentCandidateSummary(
            candidate_id=candidate.candidate_id,
            model_family=candidate.model_family,
            fold_count=len(folds),
            exact_score_negative_log_likelihood=0.8 - (index * 0.1),
            result_log_loss=0.7 - (index * 0.1),
            result_brier=0.5 - (index * 0.1),
            ranked_probability_score=0.4 - (index * 0.1),
            mean_absolute_goal_error=1.2 - (index * 0.1),
            all_folds_converged=True,
        )
        for index, candidate in enumerate(preparation._CANDIDATES)
    )
    return FootballScoreTournament(
        candidates=preparation._CANDIDATES,
        folds=folds,
        fold_metrics=metrics,
        summaries=summaries,
        provisional_winner_candidate_id=preparation._CANDIDATES[1].candidate_id,
        provisional_winner_reason="verified persisted winner",
        evaluation_provenance=EvaluationProvenance.VERIFIED_HISTORICAL,
        production_eligibility_state="production-eligible",
        production_ineligibility_reasons=(),
        multinomial_baseline_state="retained",
        market_baseline_state="unavailable",
    )


def _published_candidates(
    paths: RuntimePaths,
    tournament_artifact,
) -> dict[str, preparation._PublishedCandidate]:
    teams = ("team-a", "team-b")
    published: dict[str, preparation._PublishedCandidate] = {}
    for candidate in preparation._CANDIDATES:
        model = FootballScoreModel(
            model_family=candidate.model_family,
            competition_id="eng-premier-league",
            training_start=date(2023, 1, 1),
            training_end=date(2026, 7, 1),
            teams=teams,
            base_log_rate=0.1,
            home_advantage=0.15,
            attack_strengths=(0.0, 0.0),
            defence_strengths=(0.0, 0.0),
            rho=-0.02 if candidate.model_family == "dixon-coles" else 0.0,
            configuration=ScoreModelConfiguration(minimum_matches=1),
            diagnostics=ScoreModelDiagnostics(True, 1, 1.0, 0.0, 1),
        )
        relative = f"prepared/{candidate.candidate_id}"
        artifact = write_score_model_artifact(
            root=paths.models_directory,
            relative_directory=relative,
            model=model,
        )
        calibration_relative = f"prepared-calibration/{candidate.candidate_id}"
        calibration = write_score_calibration_artifact(
            root=paths.models_directory,
            relative_directory=calibration_relative,
            model_artifact_id=artifact.artifact_id,
            training_lineage=tournament_artifact.artifact_id,
            temperature=1.0,
        )
        published[candidate.candidate_id] = preparation._PublishedCandidate(
            candidate,
            artifact,
            model,
            relative,
            calibration,
            calibration_relative,
            1.0,
        )
    return published


def _evidence(
    published: preparation._PublishedCandidate,
    tournament_artifact,
    *,
    winner_rejected: bool,
) -> ModelEvaluationEvidence:
    is_winner = published.candidate.candidate_id == preparation._CANDIDATES[1].candidate_id
    if is_winner and not winner_rejected:
        metrics = (0.60, 0.40, 0.05)
    elif is_winner:
        metrics = (0.90, 0.70, 0.12)
    else:
        metrics = (0.80, 0.60, 0.10)
    return ModelEvaluationEvidence(
        evidence_artifact_id=tournament_artifact.artifact_id,
        evidence_checksum_sha256=tournament_artifact.checksum_sha256,
        model_artifact_id=published.artifact.artifact_id,
        sport_code="football",
        market_key=MARKET_KEY_MATCH_RESULT_1X2,
        evaluation_mode="verified-historical-rolling-origin",
        window_start_utc=datetime(2023, 1, 1, tzinfo=UTC),
        window_end_utc=datetime(2026, 7, 1, tzinfo=UTC),
        event_population_id="verified-common-population",
        sample_size=300,
        completed_result_count=300,
        coverage=1.0,
        log_loss=metrics[0],
        multiclass_brier_score=metrics[1],
        calibration_error=metrics[2],
    )


def _install_fast_boundaries(
    monkeypatch,
    paths: RuntimePaths,
    *,
    tournament: FootballScoreTournament,
    winner_rejected: bool = False,
) -> dict[str, int]:
    tournament_artifact = write_analytical_artifact(
        root=paths.exports_directory,
        relative_directory="verified/tournament",
        artifact_type="focused-verified-tournament",
        schema_version="focused-verified-tournament-v1",
        payload={"provenance": tournament.evaluation_provenance.value},
    )
    published = _published_candidates(paths, tournament_artifact)
    calls = {"tournament": 0}

    monkeypatch.setattr(
        preparation,
        "_load_competition_histories",
        lambda _paths, _references: (_history(),),
    )

    def run_tournament(*_args, **_kwargs):
        calls["tournament"] += 1
        return tournament

    monkeypatch.setattr(preparation, "run_score_tournament", run_tournament)
    monkeypatch.setattr(
        preparation,
        "_publish_tournament",
        lambda **_kwargs: tournament_artifact,
    )
    monkeypatch.setattr(
        preparation,
        "_publish_candidate",
        lambda **kwargs: published[kwargs["candidate"].candidate_id],
    )
    monkeypatch.setattr(
        preparation,
        "_governance_evidence",
        lambda **kwargs: _evidence(
            kwargs["published"],
            tournament_artifact,
            winner_rejected=winner_rejected,
        ),
    )
    return calls


def test_eligible_preparation_promotes_once_and_then_reuses(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    calls = _install_fast_boundaries(
        monkeypatch,
        paths,
        tournament=_tournament(),
    )

    first = preparation.prepare_score_champions(
        paths=paths,
        references=(),
        evaluated_at_utc=NOW,
    )
    second = preparation.prepare_score_champions(
        paths=paths,
        references=(),
        evaluated_at_utc=NOW,
    )

    assert not first.blockers
    assert len(first.champions) == 1
    assert first.champions[0].decision_id
    assert first.champions[0].transition_id
    assert second.champions[0].reused
    assert calls["tournament"] == 1
    with connect_database(paths.sqlite_path, read_only=True) as connection:
        models = connection.execute(
            "SELECT role, lifecycle_status FROM model_registry_entries"
        ).fetchall()
        transitions = connection.execute(
            "SELECT id FROM model_role_transitions WHERE transition_type = 'promotion'"
        ).fetchall()
    assert sum(row["role"] == "champion" for row in models) == 1
    assert len(transitions) == 1


def test_existing_compatible_tournament_is_strictly_reused(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    history = _persistable_history()
    tournament = _persistable_tournament(history)
    identity = content_addressed_id(
        identity_type="mvp-score-champion-preparation-v1",
        payload={
            "competition_id": history.competition_id,
            "snapshot_refs": [],
            "candidate_ids": [item.candidate_id for item in preparation._CANDIDATES],
            "split": {
                "minimum_training_rows": preparation._SPLIT.minimum_training_rows,
                "calibration_rows": preparation._SPLIT.calibration_rows,
                "test_rows": preparation._SPLIT.test_rows,
                "maximum_folds": preparation._SPLIT.maximum_folds,
            },
        },
    )
    relative = f"mvp/champion-preparation/{history.competition_id}/{identity}/tournament"
    tournament_artifact = write_tournament_artifact(
        root=paths.exports_directory,
        relative_directory=relative,
        tournament=tournament,
    )
    published = _published_candidates(paths, tournament_artifact)
    monkeypatch.setattr(
        preparation,
        "_load_competition_histories",
        lambda _paths, _references: (history,),
    )
    monkeypatch.setattr(
        preparation,
        "run_score_tournament",
        lambda *_args, **_kwargs: pytest.fail("compatible tournament must be reused"),
    )
    monkeypatch.setattr(
        preparation,
        "_publish_candidate",
        lambda **kwargs: published[kwargs["candidate"].candidate_id],
    )
    monkeypatch.setattr(
        preparation,
        "_governance_evidence",
        lambda **kwargs: _evidence(
            kwargs["published"],
            tournament_artifact,
            winner_rejected=False,
        ),
    )

    report = preparation.prepare_score_champions(
        paths=paths,
        references=(),
        evaluated_at_utc=NOW,
    )

    assert len(report.champions) == 1
    assert not report.blockers


@pytest.mark.parametrize(
    ("tournament", "blocker"),
    (
        (
            _tournament(
                eligibility="insufficient-real-evaluation-data",
                reasons=("insufficient-total-completed-matches",),
            ),
            "production-ineligible",
        ),
        (
            _tournament(provenance=EvaluationProvenance.SYNTHETIC_CONTRACT),
            "synthetic or fixture",
        ),
    ),
)
def test_ineligible_or_synthetic_tournament_cannot_activate_champion(
    tmp_path: Path,
    monkeypatch,
    tournament: FootballScoreTournament,
    blocker: str,
) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setattr(
        preparation,
        "_load_competition_histories",
        lambda _paths, _references: (_history(),),
    )
    monkeypatch.setattr(
        preparation,
        "run_score_tournament",
        lambda *_args, **_kwargs: tournament,
    )

    report = preparation.prepare_score_champions(
        paths=paths,
        references=(),
        evaluated_at_utc=NOW,
    )

    assert not report.champions
    assert blocker in report.blockers[0]
    with connect_database(paths.sqlite_path, read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_registry_entries").fetchone()[0] == 0


def test_rejected_governance_decision_persists_without_active_champion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    _install_fast_boundaries(
        monkeypatch,
        paths,
        tournament=_tournament(),
        winner_rejected=True,
    )

    reports = tuple(
        preparation.prepare_score_champions(
            paths=paths,
            references=(),
            evaluated_at_utc=NOW,
        )
        for _ in range(2)
    )

    assert all(not report.champions for report in reports)
    assert all("governance decision retain" in report.blockers[0] for report in reports)
    with connect_database(paths.sqlite_path, read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_registry_entries").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM model_registry_entries WHERE role = 'champion'"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM promotion_decisions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM model_role_transitions").fetchone()[0] == 0


def test_confirmed_prepare_system_invokes_governed_preparation_without_cli(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path)
    orchestrator = MVPOrchestrator(base_directory=tmp_path, clock=lambda: NOW)
    reference = ParticipantSourceReference(
        PARTICIPANT_SOURCE_ROLE,
        "verified/history",
        "snapshot-id",
        "a" * 64,
        "football-ingestion",
        "football-canonical-v2",
    )
    calls = 0

    def governed_preparation(**_kwargs):
        nonlocal calls
        calls += 1
        return preparation.ChampionPreparationReport(
            (
                preparation.PreparedChampion(
                    "eng-premier-league",
                    "model-id",
                    "tournament-id",
                    "decision-id",
                    "transition-id",
                    False,
                ),
            ),
            (),
        )

    monkeypatch.setattr(MVPOrchestrator, "initialize", lambda _self: {})
    monkeypatch.setattr(
        MVPOrchestrator,
        "_settings_paths",
        lambda _self: (SimpleNamespace(), paths),
    )
    monkeypatch.setattr(
        MVPOrchestrator,
        "_ensure_default_policy",
        lambda _self, _paths: None,
    )
    monkeypatch.setattr(
        MVPOrchestrator,
        "_verified_historical_references",
        lambda _self, _paths: (reference,),
    )
    monkeypatch.setattr(
        MVPOrchestrator,
        "_matching_registry",
        lambda _self, _paths, _references: SimpleNamespace(
            artifact=SimpleNamespace(artifact_id="registry-id")
        ),
    )
    monkeypatch.setattr(
        MVPOrchestrator,
        "_champions",
        staticmethod(
            lambda _paths: (
                (
                    "eng-premier-league",
                    MARKET_KEY_MATCH_RESULT_1X2,
                    "model-id",
                ),
            )
        ),
    )
    monkeypatch.setattr(
        MVPOrchestrator,
        "inspect",
        lambda _self: SimpleNamespace(
            state=MVPState.UPCOMING_EVENTS_REQUIRED,
            active_competitions=("eng-premier-league",),
        ),
    )
    monkeypatch.setattr(
        "sports_analytics.mvp.orchestrator.prepare_score_champions",
        governed_preparation,
    )

    result = orchestrator.prepare_system()

    assert calls == 1
    assert result.state is MVPState.UPCOMING_EVENTS_REQUIRED
    assert result.participant_registry_artifact_id == "registry-id"
    assert "eng-premier-league governed champion promoted" in result.actions
    assert not result.blockers
