"""Monitoring builder trust boundaries, populations, and report semantic tamper tests."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sports_analytics.artifact_serializers import (
    build_analysis_datasets,
    build_backtest_datasets,
)
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
from sports_analytics.core.exceptions import MonitoringError
from sports_analytics.core.paths import resolve_paths
from sports_analytics.core.settings import load_settings
from sports_analytics.data.codec import dumps_canonical_json, format_utc_timestamp
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.data.repositories.operations import (
    ResultSnapshotRegistrationRepository,
    SettlementRepository,
)
from sports_analytics.jobs.context import JobExecutionContext
from sports_analytics.models.identity import content_addressed_id
from sports_analytics.monitoring.artifacts import (
    MonitoringReportTrust,
    load_monitoring_report,
    publish_monitoring_report,
    verify_monitoring_report_trust,
)
from sports_analytics.monitoring.builders import build_monitoring_inputs
from sports_analytics.monitoring.contracts import (
    DEFAULT_MONITORING_POLICY,
    EvidenceReference,
    MetricDirection,
    MetricStatus,
    MetricThreshold,
    MonitoringInputs,
    evaluate_monitoring,
)
from sports_analytics.operations.handlers import run_monitoring_handler
from sports_analytics.opportunities.contracts import (
    OpportunityDecision,
    OpportunityFilter,
    opportunities_from_evaluation,
)
from sports_analytics.results.contracts import (
    EventResultStatus,
    build_football_full_match_1x2_result,
)
from sports_analytics.results.snapshots import publish_result_snapshot
from sports_analytics.services.engine_cli import main as engine_main
from sports_analytics.settlement.contracts import settle_single
from sports_analytics.settlement.service import SettlementReport, publish_settlement_report
from sports_analytics.sports.football.markets import match_result_1x2_selection
from sports_analytics.value.contracts import (
    QuoteEvaluationMode,
    evaluate_complete_market,
)
from tests.unit.support.verified_opportunities import build_test_opportunity

AS_OF = datetime(2026, 3, 2, 12, tzinfo=UTC)
START = AS_OF - timedelta(days=7)
EVENT_START = datetime(2026, 2, 28, 15, tzinfo=UTC)


def _runtime(tmp_path: Path):
    settings = load_settings(
        config_path=None,
        env_file=None,
        environ={
            "SPORTS_ANALYTICS_STORAGE__ROOT_DIRECTORY": str(tmp_path / "storage"),
            "SPORTS_ANALYTICS_STORAGE__SNAPSHOTS_DIRECTORY": str(tmp_path / "snapshots"),
            "SPORTS_ANALYTICS_STORAGE__FEATURES_DIRECTORY": str(tmp_path / "features"),
            "SPORTS_ANALYTICS_STORAGE__MODELS_DIRECTORY": str(tmp_path / "models"),
            "SPORTS_ANALYTICS_STORAGE__EXPORTS_DIRECTORY": str(tmp_path / "exports"),
            "SPORTS_ANALYTICS_STORAGE__SQLITE_PATH": str(
                tmp_path / "storage" / "operational.sqlite3"
            ),
        },
    )
    paths = resolve_paths(settings, tmp_path)
    for directory in (
        paths.snapshots_directory,
        paths.exports_directory,
        paths.models_directory,
        paths.features_directory,
        paths.sqlite_path.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    ensure_database_ready(paths.sqlite_path)
    return paths


def _analysis_artifact(paths, *, event_ids: tuple[str, ...] = ("event-1",), relative="analysis/a1"):
    predictions = []
    evaluations = []
    opportunities = []
    for index, event_id in enumerate(event_ids):
        start = EVENT_START + timedelta(hours=index)
        opportunity = build_test_opportunity(
            str(index + 1),
            event_id=event_id,
            start=start,
            source_observed_at_utc=start - timedelta(hours=1),
        )
        from tests.unit.predictions.test_second_correction_regressions import (
            _prediction_from_opportunity,
            _quote_from_opportunity,
        )

        prediction = _prediction_from_opportunity(opportunity)
        evaluation = evaluate_complete_market(
            prediction=prediction,
            quote=_quote_from_opportunity(opportunity),
            mode=QuoteEvaluationMode.LIVE_SAFE,
        )
        predictions.append(prediction)
        evaluations.append(evaluation)
        opportunities.extend(opportunities_from_evaluation(evaluation))
    filters = OpportunityFilter()
    from sports_analytics.opportunities.contracts import filter_and_rank_opportunities

    search = filter_and_rank_opportunities(tuple(opportunities), filters=filters)
    datasets = build_analysis_datasets(
        predictions=tuple(predictions),
        evaluations=tuple(evaluations),
        opportunities=tuple(opportunities),
        decisions=search.decisions,
        opportunity_rejections=search.rejected,
        combinations=(),
        combination_rejections=(),
        filters=filters,
        combination_policy_id=None,
        provenance="synthetic-contract",
    )
    return write_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory=relative,
        artifact_kind="analysis",
        schema_version="analysis-v2",
        datasets=datasets,
    )


def _register_snapshot(
    paths,
    connection,
    *,
    event_id: str,
    observed: datetime,
    relative: str,
    home_score: int = 1,
    away_score: int = 0,
    status: EventResultStatus = EventResultStatus.COMPLETED,
):
    completed = status is EventResultStatus.COMPLETED
    result = build_football_full_match_1x2_result(
        canonical_event_id=event_id,
        scheduled_start_utc=observed - timedelta(hours=2),
        event_status=status,
        source_name="synthetic-results",
        source_event_id=f"source-{event_id}-{relative}",
        source_observed_at_utc=observed,
        source_checksum_sha256=hashlib.sha256(relative.encode()).hexdigest(),
        result_provenance="monitoring-test",
        home_canonical_participant_id=f"{event_id}-home",
        away_canonical_participant_id=f"{event_id}-away",
        full_time_home_score=home_score if completed else None,
        full_time_away_score=away_score if completed else None,
        result_timestamp_utc=observed - timedelta(minutes=5) if completed else None,
    )
    snapshot = publish_result_snapshot(
        root=paths.snapshots_directory,
        relative_directory=relative,
        result=result,
    )
    ResultSnapshotRegistrationRepository(connection).register(
        snapshot=snapshot,
        registered_at=AS_OF,
        actor="test",
    )
    return snapshot


def _evidence_ref(artifact) -> dict:
    return {
        "kind": artifact.artifact_kind,
        "relative_directory": artifact.relative_directory,
        "checksum_sha256": artifact.checksum_sha256,
        "artifact_id": artifact.artifact_id,
        "schema_version": artifact.schema_version,
    }


def _policy_payload() -> dict:
    return {
        "policy_id": "local-operational-and-model-health",
        "policy_version": "monitoring-policy-v1",
        "thresholds": [
            {
                "metric_name": name,
                "warning": threshold.warning,
                "critical": threshold.critical,
                "direction": threshold.direction.value,
            }
            for name, threshold in DEFAULT_MONITORING_POLICY.thresholds
        ],
    }


def test_threshold_exact_boundaries() -> None:
    higher = MetricThreshold(10, 20, MetricDirection.HIGHER_IS_WORSE)
    assert higher.status(9.999) is MetricStatus.HEALTHY
    assert higher.status(10) is MetricStatus.WARNING
    assert higher.status(20) is MetricStatus.CRITICAL
    lower = MetricThreshold(0.9, 0.8, MetricDirection.LOWER_IS_WORSE)
    assert lower.status(0.901) is MetricStatus.HEALTHY
    assert lower.status(0.9) is MetricStatus.WARNING
    assert lower.status(0.8) is MetricStatus.CRITICAL


def test_missing_evidence_is_unknown_and_stale_data_is_critical() -> None:
    report = evaluate_monitoring(
        inputs=MonitoringInputs(
            evidence=(EvidenceReference("snapshot", "snapshot-1", "a" * 64),),
            latest_source_observed_at_utc=AS_OF - timedelta(hours=72),
        ),
        policy=DEFAULT_MONITORING_POLICY,
        as_of_utc=AS_OF,
        window_start_utc=START,
        window_end_utc=AS_OF,
    )
    by_name = {item.metric_name: item for item in report.metrics}
    assert by_name["source_observation_age_hours"].status is MetricStatus.CRITICAL
    assert by_name["result_coverage"].status is MetricStatus.UNKNOWN
    assert "missing-evidence:result_coverage" in report.incomplete_evidence_warnings


def test_backlog_lag_coverage_and_report_identity(tmp_path) -> None:
    inputs = MonitoringInputs(
        evidence=(EvidenceReference("analysis", "analysis-1", "b" * 64),),
        latest_source_observed_at_utc=AS_OF,
        artifact_failure_count=0,
        unresolved_mapping_count=0,
        incomplete_market_count=0,
        duplicate_identity_count=0,
        expected_result_count=10,
        available_result_count=9,
        settlement_candidate_count=10,
        settled_count=8,
        oldest_unsettled_completion_utc=AS_OF - timedelta(hours=48),
        prediction_count=10,
        eligible_opportunity_count=5,
        rejected_opportunity_count=5,
        probability_complete_count=10,
        quality_flag_failure_count=0,
    )
    first = evaluate_monitoring(
        inputs=inputs,
        policy=DEFAULT_MONITORING_POLICY,
        as_of_utc=AS_OF,
        window_start_utc=START,
        window_end_utc=AS_OF,
    )
    second = evaluate_monitoring(
        inputs=inputs,
        policy=DEFAULT_MONITORING_POLICY,
        as_of_utc=AS_OF,
        window_start_utc=START,
        window_end_utc=AS_OF,
    )
    assert first.run_id == second.run_id
    by_name = {item.metric_name: item for item in first.metrics}
    assert by_name["settlement_backlog_count"].value == 2
    assert by_name["settlement_lag_hours"].status is MetricStatus.CRITICAL
    assert by_name["settlement_coverage"].status is MetricStatus.CRITICAL
    artifact = publish_monitoring_report(
        root=tmp_path,
        relative_directory="monitoring/report",
        report=first,
    )
    assert (
        publish_monitoring_report(
            root=tmp_path,
            relative_directory="monitoring/report",
            report=first,
        )
        == artifact
    )
    verified = load_monitoring_report(
        root=tmp_path,
        relative_directory="monitoring/report",
        expected_checksum=artifact.checksum_sha256,
    )
    assert verified.artifact_id == artifact.artifact_id


def test_missing_and_tampered_snapshots_are_not_valid(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    analysis = _analysis_artifact(paths, event_ids=("event-1",))
    with connect_database(paths.sqlite_path) as connection:
        with transaction(connection, immediate=True):
            good = _register_snapshot(
                paths,
                connection,
                event_id="event-1",
                observed=AS_OF - timedelta(hours=1),
                relative="results/event-1",
            )
            missing = _register_snapshot(
                paths,
                connection,
                event_id="event-missing",
                observed=AS_OF - timedelta(hours=2),
                relative="results/missing",
            )
            tampered = _register_snapshot(
                paths,
                connection,
                event_id="event-tampered",
                observed=AS_OF - timedelta(hours=3),
                relative="results/tampered",
            )
        # Remove missing artifact after registration.
        missing_dir = paths.snapshots_directory / missing.relative_directory
        for child in missing_dir.iterdir():
            child.unlink()
        missing_dir.rmdir()
        # Tamper bytes while keeping sidecar identity until reload fails checksum/schema.
        tampered_dir = paths.snapshots_directory / tampered.relative_directory
        manifest = tampered_dir / "manifest.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["payload"]["result"]["source_event_id"] = "forged"
        text = dumps_canonical_json(document) + "\n"
        manifest.write_text(text, encoding="utf-8", newline="\n")
        (tampered_dir / "manifest_checksum.sha256").write_text(
            f"{hashlib.sha256(text.encode()).hexdigest()}\n",
            encoding="utf-8",
            newline="\n",
        )
        inputs = build_monitoring_inputs(
            exports_root=paths.exports_directory,
            snapshots_root=paths.snapshots_directory,
            connection=connection,
            evidence_payload=[_evidence_ref(analysis)],
            window_start_utc=START,
            window_end_utc=AS_OF,
            as_of_utc=AS_OF,
        )
    assert inputs.expected_snapshot_count == 3
    assert inputs.valid_snapshot_count == 1
    assert inputs.artifact_failure_count == 2
    assert inputs.expected_result_count == 1
    assert inputs.available_result_count == 1
    assert good.snapshot_id


def test_unrelated_snapshots_excluded_from_available_results(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    analysis = _analysis_artifact(paths, event_ids=("event-1",))
    with connect_database(paths.sqlite_path) as connection:
        with transaction(connection, immediate=True):
            _register_snapshot(
                paths,
                connection,
                event_id="event-1",
                observed=AS_OF - timedelta(hours=1),
                relative="results/event-1",
            )
            _register_snapshot(
                paths,
                connection,
                event_id="event-unrelated",
                observed=AS_OF - timedelta(hours=2),
                relative="results/unrelated",
            )
        inputs = build_monitoring_inputs(
            exports_root=paths.exports_directory,
            snapshots_root=paths.snapshots_directory,
            connection=connection,
            evidence_payload=[_evidence_ref(analysis)],
            window_start_utc=START,
            window_end_utc=AS_OF,
            as_of_utc=AS_OF,
        )
    assert inputs.expected_snapshot_count == 2
    assert inputs.valid_snapshot_count == 2
    assert inputs.expected_result_count == 1
    assert inputs.available_result_count == 1


def test_positions_backlog_and_pending_excluded_from_settled(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    analysis = _analysis_artifact(paths, event_ids=("event-1", "event-2"))
    opportunity_rows = analysis.dataset("opportunities").rows
    first_id = str(opportunity_rows[0]["opportunity_id"])
    second_id = str(opportunity_rows[1]["opportunity_id"])
    with connect_database(paths.sqlite_path) as connection:
        with transaction(connection, immediate=True):
            snapshot = _register_snapshot(
                paths,
                connection,
                event_id="event-1",
                observed=AS_OF - timedelta(hours=1),
                relative="results/event-1",
            )
            from sports_analytics.predictions.contracts import CanonicalSelectionIdentity

            home = CanonicalSelectionIdentity.from_selection(match_result_1x2_selection("home"))
            final = settle_single(
                source_artifact_id=analysis.artifact_id,
                source_artifact_checksum_sha256=analysis.checksum_sha256,
                opportunity_id=first_id,
                canonical_event_id="event-1",
                selection=home,
                decimal_odds=Decimal(str(opportunity_rows[0]["decimal_odds"])),
                result_snapshot=snapshot,
                as_of_utc=AS_OF,
            )
            # pending for second position via no-result settlement
            pending = settle_single(
                source_artifact_id=analysis.artifact_id,
                source_artifact_checksum_sha256=analysis.checksum_sha256,
                opportunity_id=second_id,
                canonical_event_id="event-2",
                selection=home,
                decimal_odds=Decimal(str(opportunity_rows[1]["decimal_odds"])),
                result_snapshot=None,
                as_of_utc=AS_OF,
            )
            # unrelated settlement row for another artifact
            unrelated = settle_single(
                source_artifact_id="9" * 64,
                source_artifact_checksum_sha256="8" * 64,
                opportunity_id="unrelated-opp",
                canonical_event_id="event-1",
                selection=home,
                decimal_odds=Decimal("2.0"),
                result_snapshot=snapshot,
                as_of_utc=AS_OF,
            )
            for settlement, suffix, as_of in (
                (final, "final", AS_OF),
                (pending, "pending", AS_OF - timedelta(minutes=1)),
                (unrelated, "unrelated", AS_OF),
            ):
                # Re-settle pending/unrelated with distinct as_of by rebuilding reports.
                adjusted = settlement
                if suffix == "pending":
                    adjusted = settle_single(
                        source_artifact_id=analysis.artifact_id,
                        source_artifact_checksum_sha256=analysis.checksum_sha256,
                        opportunity_id=second_id,
                        canonical_event_id="event-2",
                        selection=home,
                        decimal_odds=Decimal(str(opportunity_rows[1]["decimal_odds"])),
                        result_snapshot=None,
                        as_of_utc=as_of,
                    )
                run_id = content_addressed_id(
                    identity_type="analytical-settlement-run-v1",
                    payload={
                        "source_artifact_id": adjusted.source_artifact_id,
                        "source_artifact_checksum_sha256": adjusted.source_artifact_checksum_sha256,
                        "policy_id": adjusted.policy_id,
                        "policy_version": adjusted.policy_version,
                        "as_of_utc": format_utc_timestamp(adjusted.settlement_as_of_utc),
                        "settlement_ids": [adjusted.settlement_id],
                    },
                )
                published = publish_settlement_report(
                    root=paths.exports_directory,
                    relative_directory=f"settlements/{suffix}",
                    report=SettlementReport(
                        run_id=run_id,
                        source_artifact_id=adjusted.source_artifact_id,
                        source_artifact_checksum_sha256=adjusted.source_artifact_checksum_sha256,
                        policy_id=adjusted.policy_id,
                        policy_version=adjusted.policy_version,
                        as_of_utc=adjusted.settlement_as_of_utc,
                        settlements=(adjusted,),
                    ),
                )
                SettlementRepository(connection).persist_report(
                    report=published,
                    actor="test",
                    created_at=AS_OF,
                )
        inputs = build_monitoring_inputs(
            exports_root=paths.exports_directory,
            snapshots_root=paths.snapshots_directory,
            connection=connection,
            evidence_payload=[_evidence_ref(analysis)],
            window_start_utc=START,
            window_end_utc=AS_OF,
            as_of_utc=AS_OF,
        )
    assert inputs.settlement_candidate_count == len(opportunity_rows)
    assert inputs.settled_count == 1
    assert inputs.oldest_unsettled_completion_utc is not None
    assert inputs.unresolved_mapping_count is None
    assert inputs.incomplete_market_count is None
    assert inputs.duplicate_identity_count == 0
    assert inputs.quality_flag_failure_count == 0


def test_analysis_only_leaves_performance_unknown(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    analysis = _analysis_artifact(paths)
    with connect_database(paths.sqlite_path) as connection:
        inputs = build_monitoring_inputs(
            exports_root=paths.exports_directory,
            snapshots_root=paths.snapshots_directory,
            connection=connection,
            evidence_payload=[_evidence_ref(analysis)],
            window_start_utc=START,
            window_end_utc=AS_OF,
            as_of_utc=AS_OF,
        )
    report = evaluate_monitoring(
        inputs=inputs,
        policy=DEFAULT_MONITORING_POLICY,
        as_of_utc=AS_OF,
        window_start_utc=START,
        window_end_utc=AS_OF,
    )
    by_name = {item.metric_name: item for item in report.metrics}
    assert inputs.performance == ()
    assert inputs.aggregate_performance is None
    assert by_name["log_loss"].status is MetricStatus.UNKNOWN
    assert by_name["multiclass_brier_score"].status is MetricStatus.UNKNOWN


def test_verified_backtest_aggregate_performance(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    opportunity = build_test_opportunity("1", event_id="event-1", start=EVENT_START)
    from tests.unit.predictions.test_second_correction_regressions import (
        _prediction_from_opportunity,
        _quote_from_opportunity,
    )

    prediction = _prediction_from_opportunity(opportunity)
    evaluation = evaluate_complete_market(
        prediction=prediction,
        quote=_quote_from_opportunity(opportunity),
        mode=QuoteEvaluationMode.LIVE_SAFE,
    )
    filters = OpportunityFilter()
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
            roi=0.25,
            hit_rate=1.0,
            average_decimal_odds=float(opportunity.decimal_odds),
            maximum_drawdown_units=Decimal("0"),
            candidate_count=1,
            all_prediction_count=1,
            selected_prediction_count=1,
            all_log_loss=0.55,
            all_multiclass_brier_score=0.42,
        ),
        disclaimer="test",
        candidates=(SettledOpportunity(opportunity=opportunity, result=SettlementResult.WIN),),
        opportunity_decisions=(
            OpportunityDecision(
                opportunity_id=opportunity.opportunity_id,
                filter_config_id=filters.filter_config_id,
                decision_as_of_utc=opportunity.decision_as_of_utc,
                eligible=True,
                rejection_codes=(),
                accepted_rank=1,
            ),
        ),
    )
    datasets = build_backtest_datasets(
        result=result,
        predictions=(prediction,),
        evaluations=(evaluation,),
        feature_artifact_id="feature-1",
        feature_manifest_checksum_sha256="b" * 64,
        input_snapshots=(),
        random_seed=42,
        test_event_count=1,
        complete_quote_event_count=1,
        quote_coverage=1.0,
        provenance="synthetic-contract",
    )
    backtest = write_typed_analytical_artifact(
        root=paths.exports_directory,
        relative_directory="backtests/perf",
        artifact_kind="backtest",
        schema_version="football-1x2-closing-backtest-v2",
        datasets=datasets,
    )
    with connect_database(paths.sqlite_path) as connection:
        inputs = build_monitoring_inputs(
            exports_root=paths.exports_directory,
            snapshots_root=paths.snapshots_directory,
            connection=connection,
            evidence_payload=[_evidence_ref(backtest)],
            window_start_utc=START,
            window_end_utc=AS_OF,
            as_of_utc=AS_OF,
        )
    assert inputs.aggregate_performance is not None
    assert inputs.aggregate_performance.log_loss == 0.55
    assert inputs.aggregate_performance.multiclass_brier_score == 0.42
    assert inputs.aggregate_performance.hit_rate == 1.0
    assert inputs.aggregate_performance.roi == 0.25
    report = evaluate_monitoring(
        inputs=inputs,
        policy=DEFAULT_MONITORING_POLICY,
        as_of_utc=AS_OF,
        window_start_utc=START,
        window_end_utc=AS_OF,
    )
    by_name = {item.metric_name: item for item in report.metrics}
    assert by_name["log_loss"].value == 0.55
    assert by_name["multiclass_brier_score"].value == 0.42


def test_canonical_timestamp_boundaries(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    analysis = _analysis_artifact(paths)
    lower = START
    upper = AS_OF
    with connect_database(paths.sqlite_path) as connection:
        with transaction(connection, immediate=True):
            _register_snapshot(
                paths,
                connection,
                event_id="event-lower",
                observed=lower,
                relative="results/lower",
            )
            _register_snapshot(
                paths,
                connection,
                event_id="event-upper",
                observed=upper,
                relative="results/upper",
            )
            _register_snapshot(
                paths,
                connection,
                event_id="event-outside-low",
                observed=lower - timedelta(microseconds=1),
                relative="results/outside-low",
            )
            _register_snapshot(
                paths,
                connection,
                event_id="event-outside-high",
                observed=upper + timedelta(microseconds=1),
                relative="results/outside-high",
            )
        inputs = build_monitoring_inputs(
            exports_root=paths.exports_directory,
            snapshots_root=paths.snapshots_directory,
            connection=connection,
            evidence_payload=[_evidence_ref(analysis)],
            window_start_utc=lower,
            window_end_utc=upper,
            as_of_utc=upper,
        )
    assert inputs.expected_snapshot_count == 2
    assert inputs.valid_snapshot_count == 2


def test_cli_and_worker_same_run_id_and_safety(tmp_path: Path, monkeypatch, capsys) -> None:
    paths = _runtime(tmp_path)
    analysis = _analysis_artifact(paths)
    request = {
        "policy": _policy_payload(),
        "evidence": [_evidence_ref(analysis)],
        "as_of_utc": format_utc_timestamp(AS_OF),
        "window_start_utc": format_utc_timestamp(START),
        "window_end_utc": format_utc_timestamp(AS_OF),
        "output_relative_directory": "monitoring/shared",
    }
    monkeypatch.chdir(tmp_path)
    request_path = Path("monitoring-request.json")
    request_path.write_text(dumps_canonical_json(request), encoding="utf-8")
    env_path = Path("engine.env")
    env_path.write_text(
        "\n".join(
            [
                f"SPORTS_ANALYTICS_STORAGE__ROOT_DIRECTORY={paths.storage_root}",
                f"SPORTS_ANALYTICS_STORAGE__SNAPSHOTS_DIRECTORY={paths.snapshots_directory}",
                f"SPORTS_ANALYTICS_STORAGE__FEATURES_DIRECTORY={paths.features_directory}",
                f"SPORTS_ANALYTICS_STORAGE__MODELS_DIRECTORY={paths.models_directory}",
                f"SPORTS_ANALYTICS_STORAGE__EXPORTS_DIRECTORY={paths.exports_directory}",
                f"SPORTS_ANALYTICS_STORAGE__SQLITE_PATH={paths.sqlite_path}",
            ]
        ),
        encoding="utf-8",
    )
    assert (
        engine_main(["--env-file", "engine.env", "--run-monitoring", "monitoring-request.json"])
        == 0
    )
    cli_out = json.loads(capsys.readouterr().out)
    context = JobExecutionContext(
        job_id="11111111-1111-4111-8111-111111111111",
        worker_id="22222222-2222-4222-8222-222222222222",
        attempt=1,
        maximum_attempts=3,
        claimed_at=AS_OF,
        lease_expires_at=AS_OF + timedelta(minutes=5),
        logger=logging.getLogger("test"),
    )
    object.__setattr__(context, "_database_path", paths.sqlite_path)
    object.__setattr__(context, "_exports_directory", paths.exports_directory)
    object.__setattr__(context, "_snapshots_directory", paths.snapshots_directory)
    worker_payload = {
        "policy": request["policy"],
        "evidence": request["evidence"],
        "as_of_utc": request["as_of_utc"],
        "window_start_utc": request["window_start_utc"],
        "window_end_utc": request["window_end_utc"],
        "output_relative_directory": "monitoring/shared",
    }
    worker_out = run_monitoring_handler(context, worker_payload)
    assert worker_out["run_id"] == cli_out["run_id"]
    assert worker_out["report_checksum_sha256"] == cli_out["checksum_sha256"]
    # Idempotent replay does not duplicate findings.
    with connect_database(paths.sqlite_path, read_only=True) as connection:
        runs = connection.execute("SELECT COUNT(*) AS c FROM monitoring_runs").fetchone()
        findings = connection.execute("SELECT COUNT(*) AS c FROM monitoring_findings").fetchone()
        assert int(runs["c"]) == 1
        first_findings = int(findings["c"])
    run_monitoring_handler(context, worker_payload)
    with connect_database(paths.sqlite_path, read_only=True) as connection:
        runs = connection.execute("SELECT COUNT(*) AS c FROM monitoring_runs").fetchone()
        findings = connection.execute("SELECT COUNT(*) AS c FROM monitoring_findings").fetchone()
        assert int(runs["c"]) == 1
        assert int(findings["c"]) == first_findings


def test_path_traversal_and_checksum_mismatch_fail_before_publication(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    paths = _runtime(tmp_path)
    analysis = _analysis_artifact(paths)
    env_path = tmp_path / "engine.env"
    env_path.write_text(
        "\n".join(
            [
                f"SPORTS_ANALYTICS_STORAGE__ROOT_DIRECTORY={paths.storage_root}",
                f"SPORTS_ANALYTICS_STORAGE__SNAPSHOTS_DIRECTORY={paths.snapshots_directory}",
                f"SPORTS_ANALYTICS_STORAGE__FEATURES_DIRECTORY={paths.features_directory}",
                f"SPORTS_ANALYTICS_STORAGE__MODELS_DIRECTORY={paths.models_directory}",
                f"SPORTS_ANALYTICS_STORAGE__EXPORTS_DIRECTORY={paths.exports_directory}",
                f"SPORTS_ANALYTICS_STORAGE__SQLITE_PATH={paths.sqlite_path}",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    bad_path = {
        "policy": _policy_payload(),
        "evidence": [
            {
                **_evidence_ref(analysis),
                "relative_directory": "../escape/analysis",
            }
        ],
        "as_of_utc": format_utc_timestamp(AS_OF),
        "window_start_utc": format_utc_timestamp(START),
        "window_end_utc": format_utc_timestamp(AS_OF),
        "output_relative_directory": "monitoring/bad-path",
    }
    bad_checksum = {
        "policy": _policy_payload(),
        "evidence": [{**_evidence_ref(analysis), "checksum_sha256": "a" * 64}],
        "as_of_utc": format_utc_timestamp(AS_OF),
        "window_start_utc": format_utc_timestamp(START),
        "window_end_utc": format_utc_timestamp(AS_OF),
        "output_relative_directory": "monitoring/bad-checksum",
    }
    for payload, name in ((bad_path, "path.json"), (bad_checksum, "checksum.json")):
        request_path = tmp_path / name
        request_path.write_text(dumps_canonical_json(payload), encoding="utf-8")
        assert (
            engine_main(["--env-file", str(env_path), "--run-monitoring", str(request_path)]) == 2
        )
        capsys.readouterr()
        assert not (paths.exports_directory / payload["output_relative_directory"]).exists()
        with connect_database(paths.sqlite_path, read_only=True) as connection:
            assert (
                int(connection.execute("SELECT COUNT(*) AS c FROM monitoring_runs").fetchone()["c"])
                == 0
            )
            assert (
                int(
                    connection.execute("SELECT COUNT(*) AS c FROM monitoring_findings").fetchone()[
                        "c"
                    ]
                )
                == 0
            )


def test_verify_monitoring_report_requires_checksum(tmp_path: Path, monkeypatch, capsys) -> None:
    paths = _runtime(tmp_path)
    report = evaluate_monitoring(
        inputs=MonitoringInputs(
            evidence=(EvidenceReference("analysis", "analysis-1", "b" * 64),),
            artifact_failure_count=0,
            expected_result_count=1,
            available_result_count=1,
            settlement_candidate_count=1,
            settled_count=1,
            prediction_count=1,
            probability_complete_count=1,
            quality_flag_failure_count=0,
            duplicate_identity_count=0,
        ),
        policy=DEFAULT_MONITORING_POLICY,
        as_of_utc=AS_OF,
        window_start_utc=START,
        window_end_utc=AS_OF,
    )
    artifact = publish_monitoring_report(
        root=paths.exports_directory,
        relative_directory="monitoring/verify",
        report=report,
    )
    env_path = tmp_path / "engine.env"
    env_path.write_text(
        "\n".join(
            [
                f"SPORTS_ANALYTICS_STORAGE__ROOT_DIRECTORY={paths.storage_root}",
                f"SPORTS_ANALYTICS_STORAGE__SNAPSHOTS_DIRECTORY={paths.snapshots_directory}",
                f"SPORTS_ANALYTICS_STORAGE__FEATURES_DIRECTORY={paths.features_directory}",
                f"SPORTS_ANALYTICS_STORAGE__MODELS_DIRECTORY={paths.models_directory}",
                f"SPORTS_ANALYTICS_STORAGE__EXPORTS_DIRECTORY={paths.exports_directory}",
                f"SPORTS_ANALYTICS_STORAGE__SQLITE_PATH={paths.sqlite_path}",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert (
        engine_main(
            ["--env-file", str(env_path), "--verify-monitoring-report", "monitoring/verify"]
        )
        == 2
    )
    err = capsys.readouterr().err
    assert "checksum" in err
    assert '"verified":true' not in err.lower().replace(" ", "")
    assert (
        engine_main(
            [
                "--env-file",
                str(env_path),
                "--verify-monitoring-report",
                "monitoring/verify",
                "--checksum",
                artifact.checksum_sha256,
            ]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["verified"] is True
    assert out["trust_level"] == MonitoringReportTrust.EXTERNALLY_VERIFIED.value


def test_monitoring_report_trust_levels_and_semantic_tampers(tmp_path: Path) -> None:
    report = evaluate_monitoring(
        inputs=MonitoringInputs(
            evidence=(EvidenceReference("analysis", "analysis-1", "b" * 64),),
            artifact_failure_count=0,
            unresolved_mapping_count=0,
            incomplete_market_count=0,
            duplicate_identity_count=0,
            expected_result_count=2,
            available_result_count=2,
            settlement_candidate_count=2,
            settled_count=2,
            prediction_count=2,
            eligible_opportunity_count=2,
            rejected_opportunity_count=0,
            probability_complete_count=2,
            quality_flag_failure_count=0,
        ),
        policy=DEFAULT_MONITORING_POLICY,
        as_of_utc=AS_OF,
        window_start_utc=START,
        window_end_utc=AS_OF,
    )
    artifact = publish_monitoring_report(
        root=tmp_path,
        relative_directory="monitoring/trust",
        report=report,
    )
    internal = verify_monitoring_report_trust(
        root=tmp_path,
        relative_directory="monitoring/trust",
    )
    assert internal[1] is MonitoringReportTrust.INTERNALLY_CONSISTENT
    external = verify_monitoring_report_trust(
        root=tmp_path,
        relative_directory="monitoring/trust",
        expected_checksum=artifact.checksum_sha256,
        expected_run_id=report.run_id,
    )
    assert external[1] is MonitoringReportTrust.EXTERNALLY_VERIFIED
    with pytest.raises(MonitoringError):
        verify_monitoring_report_trust(
            root=tmp_path,
            relative_directory="monitoring/trust",
            expected_checksum="f" * 64,
        )

    def _rewrite(mutator) -> None:
        directory = tmp_path / "monitoring" / "trust"
        manifest = directory / "manifest.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        mutator(document)
        from sports_analytics.artifacts import build_analytical_artifact_document

        rebuilt = build_analytical_artifact_document(
            artifact_type=document["artifact_type"],
            schema_version=document["schema_version"],
            payload=document["payload"],
        )
        text = dumps_canonical_json(rebuilt) + "\n"
        manifest.write_text(text, encoding="utf-8", newline="\n")
        (directory / "manifest_checksum.sha256").write_text(
            f"{hashlib.sha256(text.encode()).hexdigest()}\n",
            encoding="utf-8",
            newline="\n",
        )

    cases = []

    def metric_value(document):
        document["payload"]["metrics"][0]["value"] = 0.123456

    cases.append(metric_value)

    def threshold(document):
        threshold_obj = document["payload"]["metrics"][0]["threshold"]
        if threshold_obj is not None:
            threshold_obj["warning"] = 0.0001

    cases.append(threshold)

    def forged_metric_id(document):
        document["payload"]["metrics"][0]["metric_id"] = "f" * 64

    cases.append(forged_metric_id)

    def missing_finding(document):
        if document["payload"]["findings"]:
            document["payload"]["findings"] = document["payload"]["findings"][1:]

    cases.append(missing_finding)

    def bad_counts(document):
        document["payload"]["counts_by_status"][0]["count"] = 99

    cases.append(bad_counts)

    def bad_summary(document):
        document["payload"]["summary_status"] = "healthy"

    cases.append(bad_summary)

    def duplicate_metric(document):
        document["payload"]["metrics"].append(document["payload"]["metrics"][0])

    cases.append(duplicate_metric)

    def reorder_metric(document):
        metrics = document["payload"]["metrics"]
        if len(metrics) > 1:
            metrics[0], metrics[1] = metrics[1], metrics[0]

    cases.append(reorder_metric)

    def duplicate_finding(document):
        findings = document["payload"]["findings"]
        if findings:
            findings.append(findings[0])

    cases.append(duplicate_finding)

    def forged_run_id(document):
        document["payload"]["run_id"] = "a" * 64

    cases.append(forged_run_id)

    for mutator in cases:
        directory = tmp_path / "monitoring" / "trust-case"
        if directory.exists():
            import shutil

            shutil.rmtree(directory)
        publish_monitoring_report(
            root=tmp_path,
            relative_directory="monitoring/trust-case",
            report=report,
        )
        manifest = directory / "manifest.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        mutator(document)
        from sports_analytics.artifacts import build_analytical_artifact_document

        rebuilt = build_analytical_artifact_document(
            artifact_type=document["artifact_type"],
            schema_version=document["schema_version"],
            payload=document["payload"],
        )
        text = dumps_canonical_json(rebuilt) + "\n"
        manifest.write_text(text, encoding="utf-8", newline="\n")
        (directory / "manifest_checksum.sha256").write_text(
            f"{hashlib.sha256(text.encode()).hexdigest()}\n",
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(MonitoringError):
            load_monitoring_report(
                root=tmp_path,
                relative_directory="monitoring/trust-case",
            )


def test_worker_rejects_conflicting_internally_consistent_report(tmp_path: Path) -> None:
    paths = _runtime(tmp_path)
    analysis = _analysis_artifact(paths)
    first = evaluate_monitoring(
        inputs=MonitoringInputs(
            evidence=(
                EvidenceReference("analysis", analysis.artifact_id, analysis.checksum_sha256),
            ),
            artifact_failure_count=0,
            expected_result_count=1,
            available_result_count=0,
            settlement_candidate_count=1,
            settled_count=0,
            prediction_count=1,
            probability_complete_count=1,
            quality_flag_failure_count=0,
            duplicate_identity_count=0,
        ),
        policy=DEFAULT_MONITORING_POLICY,
        as_of_utc=AS_OF,
        window_start_utc=START,
        window_end_utc=AS_OF,
    )
    second = evaluate_monitoring(
        inputs=MonitoringInputs(
            evidence=(
                EvidenceReference("analysis", analysis.artifact_id, analysis.checksum_sha256),
            ),
            artifact_failure_count=1,
            expected_result_count=1,
            available_result_count=0,
            settlement_candidate_count=1,
            settled_count=0,
            prediction_count=1,
            probability_complete_count=1,
            quality_flag_failure_count=0,
            duplicate_identity_count=0,
        ),
        policy=DEFAULT_MONITORING_POLICY,
        as_of_utc=AS_OF,
        window_start_utc=START,
        window_end_utc=AS_OF,
    )
    assert first.run_id != second.run_id
    publish_monitoring_report(
        root=paths.exports_directory,
        relative_directory="monitoring/conflict",
        report=first,
    )
    # Force publish of second into same directory by writing then attempting handler reuse path.
    with pytest.raises(MonitoringError):
        publish_monitoring_report(
            root=paths.exports_directory,
            relative_directory="monitoring/conflict",
            report=second,
        )


def test_positive_risk_without_threshold_is_unknown() -> None:
    report = evaluate_monitoring(
        inputs=MonitoringInputs(
            evidence=(EvidenceReference("analysis", "analysis-1", "b" * 64),),
            incomplete_market_count=3,
        ),
        policy=DEFAULT_MONITORING_POLICY,
        as_of_utc=AS_OF,
        window_start_utc=START,
        window_end_utc=AS_OF,
    )
    by_name = {item.metric_name: item for item in report.metrics}
    assert by_name["incomplete_market_count"].status is MetricStatus.UNKNOWN
