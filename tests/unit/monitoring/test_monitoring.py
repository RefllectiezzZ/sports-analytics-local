from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sports_analytics.monitoring.artifacts import (
    load_monitoring_report,
    publish_monitoring_report,
)
from sports_analytics.monitoring.contracts import (
    DEFAULT_MONITORING_POLICY,
    EvidenceReference,
    MetricDirection,
    MetricStatus,
    MetricThreshold,
    MonitoringInputs,
    evaluate_monitoring,
)

AS_OF = datetime(2026, 3, 2, 12, tzinfo=UTC)
START = AS_OF - timedelta(days=7)


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
