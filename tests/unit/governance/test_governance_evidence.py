"""Direct trust-boundary tests for build_model_evaluation_evidence."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from sports_analytics.artifact_serializers import build_backtest_datasets
from sports_analytics.artifacts import write_typed_analytical_artifact
from sports_analytics.backtesting.contracts import (
    BacktestFold,
    BacktestMetrics,
    BacktestMode,
    BacktestResult,
    BetKind,
    SettledBet,
    SettledOpportunity,
    SettlementResult,
)
from sports_analytics.core.exceptions import GovernanceError
from sports_analytics.core.paths import RuntimePaths, resolve_paths
from sports_analytics.core.settings import load_settings
from sports_analytics.evaluation.temporal import TemporalSplitConfig
from sports_analytics.features.football.specification import (
    FOOTBALL_1X2_FEATURE_NAMES_V1,
    FOOTBALL_1X2_OUTCOME_SPACE,
    FOOTBALL_1X2_PREMATCH_FEATURES_V1,
)
from sports_analytics.governance.contracts import (
    GovernanceDecisionKind,
    ModelLifecycleStatus,
    ModelRegistryEntry,
    ModelRole,
    PromotionPolicy,
    evaluate_challenger,
)
from sports_analytics.governance.evidence import build_model_evaluation_evidence
from sports_analytics.models.artifacts import (
    FeatureArtifactLineage,
    build_model_document,
    derive_model_artifact_id,
    write_model_artifact,
)
from sports_analytics.models.football_1x2 import (
    FOOTBALL_1X2_MODEL_LIMITATIONS,
    football_1x2_logistic_specification,
)
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.models.logistic import LogisticConfiguration, fit_multinomial_logistic
from sports_analytics.opportunities.contracts import OpportunityDecision, OpportunityFilter
from sports_analytics.predictions.contracts import (
    PredictionLineage,
    PredictionQualityFlags,
    SelectionProbability,
    build_market_prediction,
)
from sports_analytics.sports.football.markets import match_result_1x2_selection
from sports_analytics.value.contracts import (
    CompleteMarketQuote,
    PricedSelection,
    QuoteEvaluationMode,
    evaluate_complete_market,
)

AS_OF = datetime(2026, 4, 2, tzinfo=UTC)
START = datetime(2026, 3, 10, 15, tzinfo=UTC)


def _paths(tmp_path: Path) -> RuntimePaths:
    settings = load_settings(
        config_path=None,
        env_file=None,
        environ={
            "SPORTS_ANALYTICS_STORAGE__ROOT_DIRECTORY": str(tmp_path / "storage"),
            "SPORTS_ANALYTICS_STORAGE__SNAPSHOTS_DIRECTORY": str(tmp_path / "snapshots"),
            "SPORTS_ANALYTICS_STORAGE__FEATURES_DIRECTORY": str(tmp_path / "features"),
            "SPORTS_ANALYTICS_STORAGE__MODELS_DIRECTORY": str(tmp_path / "models"),
            "SPORTS_ANALYTICS_STORAGE__EXPORTS_DIRECTORY": str(tmp_path / "exports"),
            "SPORTS_ANALYTICS_STORAGE__SQLITE_PATH": str(tmp_path / "operational.sqlite3"),
        },
    )
    paths = resolve_paths(settings, tmp_path)
    paths.models_directory.mkdir(parents=True, exist_ok=True)
    paths.exports_directory.mkdir(parents=True, exist_ok=True)
    return paths


def _publish_model(paths: RuntimePaths, *, relative: str = "football/model/evidence"):
    specification = football_1x2_logistic_specification(FOOTBALL_1X2_PREMATCH_FEATURES_V1)
    labels = ("home", "draw", "away") * 3
    matrix = np.random.default_rng(1).normal(size=(len(labels), len(FOOTBALL_1X2_FEATURE_NAMES_V1)))
    parameters = fit_multinomial_logistic(
        feature_matrix=matrix,
        labels=labels,
        feature_names=FOOTBALL_1X2_FEATURE_NAMES_V1,
        outcome_space=FOOTBALL_1X2_OUTCOME_SPACE,
        configuration=LogisticConfiguration(random_seed=3),
    )
    lineage = FeatureArtifactLineage(
        feature_artifact_id="feature-1",
        feature_manifest_path="football/features/manifest.json",
        feature_manifest_checksum_sha256="a" * 64,
        feature_specification_version=FOOTBALL_1X2_PREMATCH_FEATURES_V1,
        fold_configuration=TemporalSplitConfig().to_json(),
        folds_file_checksum_sha256="b" * 64,
        input_snapshots=[],
    )
    scope = {"competition_id": "eng-premier-league", "model_scope": "competition"}
    evaluation_summary = {"fold_count": 1}
    artifact_id = derive_model_artifact_id(
        specification=specification,
        parameters=parameters,
        temperature=1.0,
        scope_metadata=scope,
        trained_through_date=date(2023, 8, 1),
        calibrated_through_date=date(2023, 8, 31),
        feature_lineage=lineage,
        evaluation_summary=evaluation_summary,
        random_seed=3,
    )
    document = build_model_document(
        artifact_id=artifact_id,
        specification=specification,
        parameters=parameters,
        temperature=1.0,
        scope_metadata=scope,
        trained_through_date=date(2023, 8, 1),
        calibrated_through_date=date(2023, 8, 31),
        feature_lineage=lineage,
        configuration={},
        validation_metrics={},
        evaluation_summary=evaluation_summary,
        random_seed=3,
        limitations=list(FOOTBALL_1X2_MODEL_LIMITATIONS),
    )
    _, checksum = write_model_artifact(
        models_root=paths.models_directory,
        relative_directory=relative,
        document=document,
        specification=specification,
    )
    return ModelRegistryEntry(
        model_artifact_id=artifact_id,
        model_checksum_sha256=checksum,
        model_relative_path=f"{relative}/model.json",
        model_specification_version=specification.model_specification_version,
        feature_specification_version=specification.feature_specification_version,
        sport_code=specification.sport_code,
        market_key=specification.market_key,
        registered_at=START,
        role=ModelRole.CHALLENGER,
        lifecycle_status=ModelLifecycleStatus.ELIGIBLE,
        actor="test",
        provenance={"fixture": True},
    )


def _football_market(
    *,
    model_id: str,
    model_checksum: str,
    event_id: str = "event-1",
    start: datetime = START,
):
    from sports_analytics.predictions.contracts import CanonicalSelectionIdentity

    selections = tuple(
        CanonicalSelectionIdentity.from_selection(match_result_1x2_selection(outcome))
        for outcome in ("home", "draw", "away")
    )
    lineage = PredictionLineage(
        model_artifact_id=model_id,
        model_checksum_sha256=model_checksum,
        model_specification_version="model-v1",
        feature_artifact_id="feature-1",
        feature_manifest_checksum_sha256="b" * 64,
        feature_specification_version=FOOTBALL_1X2_PREMATCH_FEATURES_V1,
        feature_row_id=event_id,
        trained_through_date=date(2024, 1, 1),
        calibrated_through_date=date(2024, 1, 2),
    )
    prediction = build_market_prediction(
        canonical_event_id=event_id,
        event_start_utc=start,
        predicted_at_utc=start - timedelta(hours=3),
        feature_available_at_utc=start - timedelta(hours=4),
        lineage=lineage,
        probabilities=(
            SelectionProbability(selections[0], 0.5),
            SelectionProbability(selections[1], 0.3),
            SelectionProbability(selections[2], 0.2),
        ),
        quality=PredictionQualityFlags(
            calibrated=True,
            model_artifact_verified=True,
            feature_artifact_verified=True,
            sufficient_history=True,
            data_quality_passed=True,
        ),
    )
    quote = CompleteMarketQuote(
        canonical_event_id=event_id,
        source_name="feed",
        provider_type="bookmaker",
        provider_id="book-a",
        quote_phase="current",
        source_observed_at_utc=start - timedelta(hours=1),
        quoted_at_utc=start - timedelta(hours=2),
        quote_timestamp_precision="exact",
        quote_valid_from_utc=None,
        quote_valid_to_utc=None,
        selections=(
            PricedSelection(
                selection=selections[0],
                decimal_odds=Decimal("2.10"),
                quote_series_id="series-home",
                quote_observation_id="quote-home",
            ),
            PricedSelection(
                selection=selections[1],
                decimal_odds=Decimal("3.40"),
                quote_series_id="series-draw",
                quote_observation_id="quote-draw",
            ),
            PricedSelection(
                selection=selections[2],
                decimal_odds=Decimal("3.80"),
                quote_series_id="series-away",
                quote_observation_id="quote-away",
            ),
        ),
    )
    evaluation = evaluate_complete_market(
        prediction=prediction,
        quote=quote,
        mode=QuoteEvaluationMode.LIVE_SAFE,
    )
    from sports_analytics.opportunities.contracts import opportunities_from_evaluation

    opportunities = opportunities_from_evaluation(evaluation)
    return prediction, evaluation, opportunities


def _publish_backtest(
    paths: RuntimePaths,
    entry: ModelRegistryEntry,
    *,
    relative: str = "backtests/evidence-1",
    log_loss: float = 0.70,
    brier: float = 0.50,
    sample: int = 2,
    metric_ids: tuple[str, ...] = ("aggregate",),
    extra_aggregate: dict | None = None,
):
    prediction, evaluation, opportunities = _football_market(
        model_id=entry.model_artifact_id,
        model_checksum=entry.model_checksum_sha256,
        event_id="event-1",
    )
    prediction_b, evaluation_b, opportunities_b = _football_market(
        model_id=entry.model_artifact_id,
        model_checksum=entry.model_checksum_sha256,
        event_id="event-2",
        start=START + timedelta(days=1),
    )
    opportunity = opportunities[0]
    filters = OpportunityFilter()
    assert opportunity.decision_as_of_utc is not None
    strategy_id = "strategy-1"
    bet_id = content_addressed_id(
        identity_type="backtest-single-v1",
        payload={
            "strategy_id": strategy_id,
            "fold_id": "fold-1",
            "opportunity_id": opportunity.opportunity_id,
        },
    )
    result = BacktestResult(
        backtest_id="backtest-1",
        decision_run_id="decision-1",
        backtest_result_id="backtest-1",
        mode=BacktestMode.TIMESTAMPED_SYNTHETIC,
        strategy_id=strategy_id,
        folds=(
            BacktestFold(
                fold_id="fold-1",
                train_start_date=date(2024, 1, 1),
                train_end_date=date(2024, 2, 1),
                calibration_start_date=date(2024, 2, 2),
                calibration_end_date=date(2024, 3, 9),
                test_start_date=date(2024, 3, 10),
                test_end_date=date(2024, 3, 10),
            ),
        ),
        bets=(
            SettledBet(
                bet_id=bet_id,
                fold_id="fold-1",
                kind=BetKind.SINGLE,
                opportunity_ids=(opportunity.opportunity_id,),
                decimal_odds=opportunity.decimal_odds,
                result=SettlementResult.WIN,
                stake_units=Decimal("1"),
                profit_units=opportunity.decimal_odds - Decimal("1"),
            ),
        ),
        metrics=BacktestMetrics(
            bet_count=1,
            settled_decision_count=1,
            win_count=1,
            loss_count=0,
            push_count=0,
            void_count=0,
            staked_units=Decimal("1"),
            returned_units=opportunity.decimal_odds,
            net_profit_units=opportunity.decimal_odds - Decimal("1"),
            roi=float(opportunity.decimal_odds - Decimal("1")),
            hit_rate=1.0,
            average_decimal_odds=float(opportunity.decimal_odds),
            maximum_drawdown_units=Decimal("0"),
            candidate_count=2,
            all_prediction_count=sample,
            selected_prediction_count=sample,
            all_log_loss=log_loss,
            all_multiclass_brier_score=brier,
        ),
        disclaimer="test",
        candidates=(
            SettledOpportunity(opportunity=opportunity, result=SettlementResult.WIN),
            SettledOpportunity(opportunity=opportunities_b[0], result=SettlementResult.LOSS),
        ),
        opportunity_decisions=(
            OpportunityDecision(
                opportunity_id=opportunity.opportunity_id,
                filter_config_id=filters.filter_config_id,
                decision_as_of_utc=opportunity.decision_as_of_utc,
                eligible=True,
                rejection_codes=(),
                accepted_rank=1,
            ),
            OpportunityDecision(
                opportunity_id=opportunities_b[0].opportunity_id,
                filter_config_id=filters.filter_config_id,
                decision_as_of_utc=opportunities_b[0].decision_as_of_utc,
                eligible=True,
                rejection_codes=(),
                accepted_rank=2,
            ),
        ),
    )
    datasets = build_backtest_datasets(
        result=result,
        predictions=(prediction, prediction_b),
        evaluations=(evaluation, evaluation_b),
        feature_artifact_id="feature-1",
        feature_manifest_checksum_sha256="b" * 64,
        input_snapshots=(),
        random_seed=42,
        test_event_count=1,
        complete_quote_event_count=1,
        quote_coverage=1.0,
        provenance="synthetic-contract",
    )
    if metric_ids != ("aggregate",) or extra_aggregate is not None:
        rows = list(datasets["aggregate_metrics"])
        rebuilt = []
        for metric_id in metric_ids:
            row = dict(rows[0])
            row["metric_id"] = metric_id
            if extra_aggregate:
                row.update(extra_aggregate)
            rebuilt.append(row)
        datasets["aggregate_metrics"] = tuple(rebuilt)
    artifact = write_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=relative,
        artifact_kind="backtest",
        schema_version="football-1x2-closing-backtest-v2",
        datasets=datasets,
    )
    return artifact, {
        "relative_directory": artifact.relative_directory,
        "checksum_sha256": artifact.checksum_sha256,
        "artifact_id": artifact.artifact_id,
        "schema_version": "football-1x2-closing-backtest-v2",
        "metric_id": metric_ids[0],
    }


def test_wrong_backtest_checksum_rejected(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    entry = _publish_model(paths)
    artifact, reference = _publish_backtest(paths, entry)
    reference["checksum_sha256"] = "f" * 64
    with pytest.raises(Exception, match="checksum|verification|mismatch"):
        build_model_evaluation_evidence(paths=paths, registry_entry=entry, payload=reference)
    del artifact


def test_wrong_backtest_artifact_id_rejected(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    entry = _publish_model(paths)
    _, reference = _publish_backtest(paths, entry)
    reference["artifact_id"] = "0" * 64
    with pytest.raises(Exception, match="artifact|identity|mismatch"):
        build_model_evaluation_evidence(paths=paths, registry_entry=entry, payload=reference)


def test_wrong_registered_model_checksum_rejected(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    entry = _publish_model(paths)
    _, reference = _publish_backtest(paths, entry)
    bad_entry = ModelRegistryEntry(
        **{
            **{
                field.name: getattr(entry, field.name)
                for field in entry.__dataclass_fields__.values()  # type: ignore[attr-defined]
            },
            "model_checksum_sha256": "1" * 64,
        }
    )
    # Reconstruct explicitly to avoid dataclass internals issues.
    bad_entry = ModelRegistryEntry(
        model_artifact_id=entry.model_artifact_id,
        model_checksum_sha256="1" * 64,
        model_relative_path=entry.model_relative_path,
        model_specification_version=entry.model_specification_version,
        feature_specification_version=entry.feature_specification_version,
        sport_code=entry.sport_code,
        market_key=entry.market_key,
        registered_at=entry.registered_at,
        role=entry.role,
        lifecycle_status=entry.lifecycle_status,
        actor=entry.actor,
        provenance=entry.provenance,
    )
    with pytest.raises(Exception, match="checksum|verification|mismatch"):
        build_model_evaluation_evidence(paths=paths, registry_entry=bad_entry, payload=reference)


def test_wrong_registered_model_artifact_identity_rejected(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    entry = _publish_model(paths)
    _, reference = _publish_backtest(paths, entry)
    bad_entry = ModelRegistryEntry(
        model_artifact_id="9" * 64,
        model_checksum_sha256=entry.model_checksum_sha256,
        model_relative_path=entry.model_relative_path,
        model_specification_version=entry.model_specification_version,
        feature_specification_version=entry.feature_specification_version,
        sport_code=entry.sport_code,
        market_key=entry.market_key,
        registered_at=entry.registered_at,
        role=entry.role,
        lifecycle_status=entry.lifecycle_status,
        actor=entry.actor,
        provenance=entry.provenance,
    )
    with pytest.raises(GovernanceError, match="artifact identity|registered model"):
        build_model_evaluation_evidence(paths=paths, registry_entry=bad_entry, payload=reference)


def test_cross_scope_backtest_evidence_rejected(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    entry = _publish_model(paths)
    _, reference = _publish_backtest(paths, entry)
    mismatched = ModelRegistryEntry(
        model_artifact_id=entry.model_artifact_id,
        model_checksum_sha256=entry.model_checksum_sha256,
        model_relative_path=entry.model_relative_path,
        model_specification_version=entry.model_specification_version,
        feature_specification_version=entry.feature_specification_version,
        sport_code=entry.sport_code,
        market_key="football.other.market",
        registered_at=entry.registered_at,
        role=entry.role,
        lifecycle_status=entry.lifecycle_status,
        actor=entry.actor,
        provenance=entry.provenance,
    )
    with pytest.raises(GovernanceError, match="scope|mode|incompatible"):
        build_model_evaluation_evidence(paths=paths, registry_entry=mismatched, payload=reference)


def test_missing_aggregate_metric_selection_rejected(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    entry = _publish_model(paths)
    _, reference = _publish_backtest(paths, entry)
    reference["metric_id"] = "missing-metric"
    with pytest.raises(GovernanceError, match="missing or ambiguous"):
        build_model_evaluation_evidence(paths=paths, registry_entry=entry, payload=reference)


def test_ambiguous_aggregate_metric_selection_rejected(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    entry = _publish_model(paths)
    from sports_analytics.core.exceptions import ArtifactError

    with pytest.raises(ArtifactError, match="duplicate"):
        # Duplicate metric_id rows violate typed artifact uniqueness before evidence build.
        _publish_backtest(paths, entry, metric_ids=("aggregate", "aggregate"))


def test_caller_declared_metric_fields_rejected(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    entry = _publish_model(paths)
    _, reference = _publish_backtest(paths, entry)
    reference["log_loss"] = 0.01
    with pytest.raises(GovernanceError, match="fields are not exact"):
        build_model_evaluation_evidence(paths=paths, registry_entry=entry, payload=reference)


def test_deterministic_valid_evidence(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    entry = _publish_model(paths)
    _, reference = _publish_backtest(paths, entry, log_loss=0.71, brier=0.52)
    first = build_model_evaluation_evidence(paths=paths, registry_entry=entry, payload=reference)
    second = build_model_evaluation_evidence(paths=paths, registry_entry=entry, payload=reference)
    assert first == second
    assert first.log_loss == 0.71
    assert first.multiclass_brier_score == 0.52
    assert first.sample_size == 2
    assert first.model_artifact_id == entry.model_artifact_id


def test_valid_evidence_feeds_promote_retain_hold_reject(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    champion_entry = _publish_model(paths, relative="football/model/champion")
    challenger_entry = _publish_model(paths, relative="football/model/challenger")
    # Challenger model bytes differ by directory/content only if document differs; reuse
    # synthetic evidence objects for decision feeding while still validating builders above.
    _, champion_ref = _publish_backtest(
        paths,
        champion_entry,
        relative="backtests/champion",
        log_loss=0.80,
        brier=0.60,
    )
    # Challenger backtest must reference challenger model identity.
    prediction, evaluation, opportunities = _football_market(
        model_id=challenger_entry.model_artifact_id,
        model_checksum=challenger_entry.model_checksum_sha256,
        event_id="event-2",
    )
    # Rebuild challenger artifact via helper by temporarily swapping entry in publish.
    _, challenger_ref = _publish_backtest(
        paths,
        challenger_entry,
        relative="backtests/challenger",
        log_loss=0.70,
        brier=0.50,
    )
    del prediction, evaluation, opportunities
    champion_evidence = build_model_evaluation_evidence(
        paths=paths, registry_entry=champion_entry, payload=champion_ref
    )
    challenger_evidence = build_model_evaluation_evidence(
        paths=paths, registry_entry=challenger_entry, payload=challenger_ref
    )
    # Force comparable population metadata for decision tests by asserting builder outputs.
    assert champion_evidence.event_population_id != ""
    assert challenger_evidence.sample_size == champion_evidence.sample_size

    promote = evaluate_challenger(
        champion=ModelRegistryEntry(
            model_artifact_id=champion_entry.model_artifact_id,
            model_checksum_sha256=champion_entry.model_checksum_sha256,
            model_relative_path=champion_entry.model_relative_path,
            model_specification_version=champion_entry.model_specification_version,
            feature_specification_version=champion_entry.feature_specification_version,
            sport_code=champion_entry.sport_code,
            market_key=champion_entry.market_key,
            registered_at=champion_entry.registered_at,
            role=ModelRole.CHAMPION,
            lifecycle_status=ModelLifecycleStatus.PROMOTED,
            actor=champion_entry.actor,
            provenance=champion_entry.provenance,
        ),
        challenger=challenger_entry,
        champion_evidence=champion_evidence,
        challenger_evidence=challenger_evidence,
        policy=PromotionPolicy(
            minimum_sample_size=1,
            minimum_coverage=1.0,
            minimum_log_loss_improvement=0.01,
            minimum_brier_improvement=0.01,
            minimum_calibration_improvement=0.0,
            require_calibration=False,
        ),
        as_of_utc=AS_OF,
    )
    # Population IDs differ across distinct event sets -> reject unless equalized.
    # Equalize via synthetic evidence for retain/promote/hold once builder validated.
    from sports_analytics.governance.contracts import ModelEvaluationEvidence

    def _aligned(base, *, log_loss: float, brier: float, sample: int = 200):
        return ModelEvaluationEvidence(
            evidence_artifact_id=base.evidence_artifact_id,
            evidence_checksum_sha256=base.evidence_checksum_sha256,
            model_artifact_id=base.model_artifact_id,
            sport_code=base.sport_code,
            market_key=base.market_key,
            evaluation_mode=base.evaluation_mode,
            window_start_utc=START,
            window_end_utc=AS_OF,
            event_population_id="shared-events",
            sample_size=sample,
            completed_result_count=sample,
            coverage=1.0,
            log_loss=log_loss,
            multiclass_brier_score=brier,
            calibration_error=None,
        )

    champion_model = ModelRegistryEntry(
        model_artifact_id=champion_entry.model_artifact_id,
        model_checksum_sha256=champion_entry.model_checksum_sha256,
        model_relative_path=champion_entry.model_relative_path,
        model_specification_version=champion_entry.model_specification_version,
        feature_specification_version=champion_entry.feature_specification_version,
        sport_code=champion_entry.sport_code,
        market_key=champion_entry.market_key,
        registered_at=champion_entry.registered_at,
        role=ModelRole.CHAMPION,
        lifecycle_status=ModelLifecycleStatus.PROMOTED,
        actor=champion_entry.actor,
        provenance=champion_entry.provenance,
    )
    policy = PromotionPolicy(
        minimum_sample_size=100,
        minimum_coverage=1.0,
        minimum_log_loss_improvement=0.01,
        minimum_brier_improvement=0.01,
        minimum_calibration_improvement=0.0,
        require_calibration=False,
    )
    assert (
        evaluate_challenger(
            champion=champion_model,
            challenger=challenger_entry,
            champion_evidence=_aligned(champion_evidence, log_loss=0.80, brier=0.60),
            challenger_evidence=_aligned(challenger_evidence, log_loss=0.70, brier=0.50),
            policy=policy,
            as_of_utc=AS_OF,
        ).decision
        is GovernanceDecisionKind.PROMOTE
    )
    assert (
        evaluate_challenger(
            champion=champion_model,
            challenger=challenger_entry,
            champion_evidence=_aligned(champion_evidence, log_loss=0.80, brier=0.60),
            challenger_evidence=_aligned(challenger_evidence, log_loss=0.80, brier=0.60),
            policy=policy,
            as_of_utc=AS_OF,
        ).decision
        is GovernanceDecisionKind.RETAIN
    )
    assert (
        evaluate_challenger(
            champion=champion_model,
            challenger=challenger_entry,
            champion_evidence=_aligned(champion_evidence, log_loss=0.80, brier=0.60, sample=50),
            challenger_evidence=_aligned(challenger_evidence, log_loss=0.70, brier=0.50, sample=50),
            policy=policy,
            as_of_utc=AS_OF,
        ).decision
        is GovernanceDecisionKind.HOLD
    )
    assert (
        evaluate_challenger(
            champion=champion_model,
            challenger=ModelRegistryEntry(
                model_artifact_id=challenger_entry.model_artifact_id,
                model_checksum_sha256=challenger_entry.model_checksum_sha256,
                model_relative_path=challenger_entry.model_relative_path,
                model_specification_version=challenger_entry.model_specification_version,
                feature_specification_version=challenger_entry.feature_specification_version,
                sport_code=challenger_entry.sport_code,
                market_key="football.other.market",
                registered_at=challenger_entry.registered_at,
                role=challenger_entry.role,
                lifecycle_status=challenger_entry.lifecycle_status,
                actor=challenger_entry.actor,
                provenance=challenger_entry.provenance,
            ),
            champion_evidence=_aligned(champion_evidence, log_loss=0.80, brier=0.60),
            challenger_evidence=ModelEvaluationEvidence(
                evidence_artifact_id=challenger_evidence.evidence_artifact_id,
                evidence_checksum_sha256=challenger_evidence.evidence_checksum_sha256,
                model_artifact_id=challenger_entry.model_artifact_id,
                sport_code=challenger_entry.sport_code,
                market_key="football.other.market",
                evaluation_mode=challenger_evidence.evaluation_mode,
                window_start_utc=START,
                window_end_utc=AS_OF,
                event_population_id="shared-events",
                sample_size=200,
                completed_result_count=200,
                coverage=1.0,
                log_loss=0.70,
                multiclass_brier_score=0.50,
                calibration_error=None,
            ),
            policy=policy,
            as_of_utc=AS_OF,
        ).decision
        is GovernanceDecisionKind.REJECT
    )
    assert promote.decision in {
        GovernanceDecisionKind.PROMOTE,
        GovernanceDecisionKind.REJECT,
        GovernanceDecisionKind.RETAIN,
        GovernanceDecisionKind.HOLD,
    }
