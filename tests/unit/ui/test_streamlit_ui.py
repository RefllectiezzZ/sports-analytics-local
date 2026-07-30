"""Focused trust-boundary, view-model, combination, theme, and smoke tests."""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from sports_analytics.artifact_schemas import (
    AGGREGATE_METRICS_SCHEMA_VERSION,
    COMBINATIONS_SCHEMA_VERSION,
)
from sports_analytics.artifacts import (
    TypedAnalyticalArtifact,
    TypedDataset,
    write_typed_analytical_artifact,
)
from sports_analytics.combinations.contracts import (
    CombinationRules,
    validate_combination_manual,
)
from sports_analytics.core.exceptions import ArtifactError
from sports_analytics.services.combinations_trusted import (
    opportunities_from_typed_artifact,
)
from sports_analytics.ui import theme
from sports_analytics.ui.catalogue import (
    discover_typed_artifacts,
    load_catalogue_artifact,
)
from sports_analytics.ui.view_models import (
    CombinationDisplayRow,
    OpportunityFilters,
    build_artifact_summary,
    build_backtest_display,
    build_combination_rows,
    build_opportunity_rows,
    filter_combinations_by_odds,
    filter_opportunities,
    select_provenance_warning,
)
from tests.unit.artifacts.test_analytical_artifacts import _typed_datasets
from tests.unit.support.verified_opportunities import (
    basketball_selection,
    build_test_opportunity,
)


def _write_analysis(root: Path, relative: str) -> TypedAnalyticalArtifact:
    return write_typed_analytical_artifact(
        root=root,
        relative_directory=relative,
        artifact_kind="analysis",
        schema_version="analysis-v2",
        datasets=_typed_datasets(),
    )


def _write_backtest(root: Path, relative: str) -> TypedAnalyticalArtifact:
    datasets = _typed_datasets()
    datasets["settlements"] = ()
    datasets["fold_metrics"] = ()
    datasets["aggregate_metrics"] = (_aggregate_metrics_row(),)
    return write_typed_analytical_artifact(
        root=root,
        relative_directory=relative,
        artifact_kind="backtest",
        schema_version="football-1x2-closing-backtest-v2",
        datasets=datasets,
    )


def _aggregate_metrics_row() -> dict[str, object]:
    return {
        "metric_id": "aggregate",
        "schema_version": AGGREGATE_METRICS_SCHEMA_VERSION,
        "backtest_id": "backtest-1",
        "decision_run_id": "decision-1",
        "mode": "timestamped-synthetic",
        "strategy_id": "strategy-1",
        "feature_artifact_id": "feature-1",
        "feature_manifest_checksum_sha256": "b" * 64,
        "input_snapshots": [],
        "random_seed": 42,
        "test_event_count": 1,
        "complete_quote_event_count": 1,
        "quote_coverage": 1.0,
        "candidate_count": 2,
        "rejection_count": 1,
        "accepted_single_count": 1,
        "accepted_combination_count": 0,
        "bet_count": 1,
        "win_count": 1,
        "loss_count": 0,
        "push_count": 0,
        "void_count": 0,
        "staked_units": "1",
        "returned_units": "2",
        "gross_return_units": "2",
        "net_profit_units": "1",
        "roi": 1.0,
        "hit_rate": 1.0,
        "average_decimal_odds": 2.0,
        "maximum_drawdown_units": "0",
        "cumulative_profit_units": ["0", "1"],
        "average_model_probability": 0.6,
        "average_edge": 0.1,
        "average_expected_value": 0.2,
        "all_prediction_count": 1,
        "selected_prediction_count": 1,
        "all_log_loss": 0.5,
        "all_multiclass_brier_score": 0.2,
        "selected_log_loss": 0.5,
        "selected_multiclass_brier_score": 0.2,
        "rejection_counts_by_reason": [["edge", 1, 2]],
        "disclaimer": "Persisted synthetic benchmark.",
    }


def test_catalogue_is_empty_for_absent_or_empty_root(tmp_path: Path) -> None:
    assert discover_typed_artifacts(tmp_path / "missing") == ()
    root = tmp_path / "exports"
    root.mkdir()
    assert discover_typed_artifacts(root) == ()


def test_catalogue_ordering_is_deterministic_and_selected_artifact_reloads(
    tmp_path: Path,
) -> None:
    root = tmp_path / "exports"
    _write_analysis(root, "analysis/z-last")
    first = _write_analysis(root, "analysis/a-first")
    entries = discover_typed_artifacts(root)
    assert [entry.relative_directory for entry in entries] == [
        "analysis/a-first",
        "analysis/z-last",
    ]
    loaded = load_catalogue_artifact(root=root, entry=entries[0])
    assert loaded.artifact_id == first.artifact_id
    assert loaded.dataset("opportunities").row_count == 2


def test_malformed_artifact_is_reported_and_never_trusted(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    candidate = root / "analysis" / "broken"
    candidate.mkdir(parents=True)
    (candidate / "manifest.json").write_text('{"broken":', encoding="utf-8")
    entries = discover_typed_artifacts(root)
    assert len(entries) == 1
    assert not entries[0].is_valid
    assert entries[0].validation_error
    with pytest.raises(ArtifactError, match="invalid catalogue"):
        load_catalogue_artifact(root=root, entry=entries[0])


def test_catalogue_metadata_never_bypasses_typed_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "exports"
    candidate = root / "analysis" / "untrusted"
    candidate.mkdir(parents=True)
    (candidate / "manifest.json").write_text(
        json.dumps(
            {
                "artifact_kind": "analysis",
                "schema_version": "analysis-v2",
            }
        ),
        encoding="utf-8",
    )

    def reject_loader(**_kwargs: object) -> TypedAnalyticalArtifact:
        raise ArtifactError("strict loader called")

    monkeypatch.setattr(
        "sports_analytics.ui.catalogue.load_typed_analytical_artifact",
        reject_loader,
    )
    entry = discover_typed_artifacts(root)[0]
    assert not entry.is_valid
    assert entry.validation_error == "strict loader called"


def test_typed_analysis_and_backtest_catalogue_loading(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    analysis = _write_analysis(root, "analysis/id-1")
    backtest = _write_backtest(root, "backtests/id-1")
    entries = discover_typed_artifacts(root)
    assert {entry.artifact_kind for entry in entries} == {"analysis", "backtest"}
    loaded = {
        entry.artifact_kind: load_catalogue_artifact(root=root, entry=entry) for entry in entries
    }
    assert loaded["analysis"].artifact_id == analysis.artifact_id
    assert loaded["backtest"].artifact_id == backtest.artifact_id


def test_opportunity_conversion_links_decisions_and_is_deterministic(
    tmp_path: Path,
) -> None:
    artifact = _write_analysis(tmp_path, "analysis/id-1")
    first = build_opportunity_rows(artifact)
    second = build_opportunity_rows(artifact)
    assert first == second
    assert len(first) == 2
    assert {row.decision_status for row in first} == {"rejected"}
    assert all(row.accepted_rank is None for row in first)
    assert all("expected-value" in row.rejection_codes for row in first)
    assert all(row.selection for row in first)
    assert all(row.evaluation_id for row in first)
    assert all(row.selection_id for row in first)
    assert all(row.complete_market_raw_total > 1.0 for row in first)
    assert {row.provenance for row in first} == {"synthetic-contract"}


def test_opportunity_filters_cover_sport_provider_date_odds_edge_and_ev(
    tmp_path: Path,
) -> None:
    base = build_opportunity_rows(_write_analysis(tmp_path, "analysis/id-1"))[0]
    other = replace(
        base,
        opportunity_id="other-opportunity",
        canonical_event_id="event-2",
        event_start_utc="2024-03-12T15:00:00.000000Z",
        sport="basketball",
        provider_id="provider-b",
        decimal_odds=3.5,
        edge=0.2,
        expected_value=0.3,
        decision_status="rejected",
        rejection_codes=("provider",),
    )
    rows = (base, other)
    selected = filter_opportunities(
        rows,
        OpportunityFilters(
            search="event-2",
            sports=("basketball",),
            providers=("provider-b",),
            event_date_from=date(2024, 3, 12),
            event_date_to=date(2024, 3, 12),
            decision_status="rejected",
            minimum_decimal_odds=3.0,
            maximum_decimal_odds=4.0,
            minimum_model_probability=0.4,
            minimum_edge=0.15,
            minimum_expected_value=0.25,
        ),
    )
    assert selected == (other,)


def test_per_leg_and_total_odds_filters_are_independent(tmp_path: Path) -> None:
    leg = build_opportunity_rows(_write_analysis(tmp_path, "analysis/id-1"))[0]
    second_leg = replace(leg, opportunity_id="leg-2", decimal_odds=3.0)
    combination = CombinationDisplayRow(
        combination_id="combo-1",
        policy_id="policy-1",
        policy_version="v1",
        opportunity_ids=(leg.opportunity_id, second_leg.opportunity_id),
        legs=(leg, second_leg),
        dependencies=(),
        total_decimal_odds=5.7,
        joint_probability=0.2,
        expected_value=0.14,
        common_decision_time_utc="2024-02-10T12:00:00.000000Z",
        earliest_event_start_utc="2024-02-10T15:00:00.000000Z",
        latest_event_start_utc="2024-02-11T15:00:00.000000Z",
        eligible=True,
        rejection_reasons=(),
    )
    assert filter_combinations_by_odds(
        (combination,),
        leg_minimum_odds=1.5,
        leg_maximum_odds=3.1,
        total_minimum_odds=5.0,
        total_maximum_odds=6.0,
    ) == (combination,)
    assert not filter_combinations_by_odds(
        (combination,),
        leg_minimum_odds=1.5,
        leg_maximum_odds=2.5,
        total_minimum_odds=5.0,
        total_maximum_odds=6.0,
    )
    assert not filter_combinations_by_odds(
        (combination,),
        leg_minimum_odds=1.5,
        leg_maximum_odds=3.1,
        total_minimum_odds=6.0,
        total_maximum_odds=7.0,
    )


def test_manual_preview_accepts_mixed_sport_and_mixed_date_separate_legs() -> None:
    first = build_test_opportunity(
        "one",
        event_id="event-1",
        start=datetime(2024, 3, 10, 15, tzinfo=UTC),
    )
    second = build_test_opportunity(
        "two",
        event_id="event-2",
        start=datetime(2024, 3, 12, 15, tzinfo=UTC),
        selection=basketball_selection(
            sport_code="tennis",
            market_key="tennis.match-winner.full-match",
        ),
        quoted=datetime(2024, 3, 10, 11, tzinfo=UTC),
        predicted_at_utc=datetime(2024, 3, 10, 10, tzinfo=UTC),
        source_observed_at_utc=datetime(2024, 3, 10, 12, tzinfo=UTC),
    )
    result = validate_combination_manual((first, second), rules=_two_leg_rules())
    assert result.eligible
    assert result.combination is not None
    assert {leg.selection.sport_code for leg in result.combination.legs} == {
        "basketball",
        "tennis",
    }
    assert len({leg.event_start_utc.date() for leg in result.combination.legs}) == 2


def test_manual_preview_rejects_conflict_and_unknown_dependency() -> None:
    first = build_test_opportunity("one", event_id="same-event")
    conflict = build_test_opportunity(
        "two",
        event_id="same-event",
        selection=basketball_selection(outcome="b"),
    )
    conflict_result = validate_combination_manual(
        (first, conflict),
        rules=_two_leg_rules(),
    )
    assert not conflict_result.eligible
    assert "conflicting legs" in conflict_result.rejection_reasons[0]

    unknown = build_test_opportunity(
        "three",
        event_id="other-event",
        dependency_metadata_complete=False,
    )
    unknown_result = validate_combination_manual(
        (first, unknown),
        rules=_two_leg_rules(),
    )
    assert not unknown_result.eligible
    assert "unknown dependency rejected" in unknown_result.rejection_reasons[0]


def test_verified_artifact_opportunities_project_through_domain_service(
    tmp_path: Path,
) -> None:
    artifact = _write_analysis(tmp_path, "analysis/id-1")
    eligible = opportunities_from_typed_artifact(artifact, eligible_only=True)
    all_decided = opportunities_from_typed_artifact(artifact, eligible_only=False)
    assert eligible == ()
    assert len(all_decided) == 2


def test_persisted_combination_display_conversion() -> None:
    first = build_test_opportunity("one", event_id="event-1")
    second = build_test_opportunity(
        "two",
        event_id="event-2",
        start=first.event_start_utc + timedelta(days=1),
        quoted=first.event_start_utc - timedelta(hours=4),
        predicted_at_utc=first.event_start_utc - timedelta(hours=5),
        source_observed_at_utc=first.event_start_utc - timedelta(hours=3),
    )
    validated = validate_combination_manual((first, second), rules=_two_leg_rules())
    assert validated.combination is not None
    from sports_analytics.artifact_serializers import (
        serialize_combination_row,
        serialize_opportunity_row,
    )

    opportunity_rows = tuple(serialize_opportunity_row(item) for item in (first, second))
    combination_row = serialize_combination_row(validated.combination)
    artifact = TypedAnalyticalArtifact(
        relative_directory="analysis/in-memory",
        artifact_id="artifact-1",
        artifact_kind="analysis",
        schema_version="analysis-v2",
        checksum_sha256="a" * 64,
        datasets=(
            TypedDataset(
                name="opportunities",
                filename="opportunities.jsonl",
                schema_version="opportunities-v2",
                id_field="opportunity_id",
                row_count=2,
                checksum_sha256="b" * 64,
                rows=opportunity_rows,
            ),
            TypedDataset(
                name="opportunity_decisions",
                filename="opportunity_decisions.jsonl",
                schema_version="opportunity-decisions-v2",
                id_field="opportunity_id",
                row_count=0,
                checksum_sha256="c" * 64,
                rows=(),
            ),
            TypedDataset(
                name="rejections",
                filename="rejections.jsonl",
                schema_version="rejections-v2",
                id_field="rejection_id",
                row_count=0,
                checksum_sha256="d" * 64,
                rows=(),
            ),
            TypedDataset(
                name="combinations",
                filename="combinations.jsonl",
                schema_version=COMBINATIONS_SCHEMA_VERSION,
                id_field="combination_id",
                row_count=1,
                checksum_sha256="e" * 64,
                rows=(combination_row,),
            ),
        ),
    )
    row = build_combination_rows(artifact)[0]
    assert row.opportunity_ids == tuple(sorted((first.opportunity_id, second.opportunity_id)))
    assert len(row.legs) == 2
    assert all(item["classification"] == "structurally_separate" for item in row.dependencies)


def test_backtest_metrics_are_displayed_from_persisted_values(tmp_path: Path) -> None:
    artifact = _write_backtest(tmp_path, "backtests/id-1")
    display = build_backtest_display(artifact)
    assert display.aggregate_metrics[0]["roi"] == 1.0
    assert display.cumulative_profit_units == (0.0, 1.0)
    assert display.settlements == ()


@pytest.mark.parametrize(
    ("provenances", "modes", "code"),
    (
        (("synthetic-contract",), ("live-safe",), "synthetic-contract"),
        (("historical-replay",), ("live-safe",), "historical-replay"),
        (
            ("historical-replay",),
            ("closing-line-historical-benchmark",),
            "closing-line-historical-benchmark",
        ),
    ),
)
def test_provenance_warning_selection(
    provenances: tuple[str, ...],
    modes: tuple[str, ...],
    code: str,
) -> None:
    assert (
        select_provenance_warning(
            provenances=provenances,
            evaluation_modes=modes,
        ).code
        == code
    )


def test_summary_and_view_models_repeat_deterministically(tmp_path: Path) -> None:
    artifact = _write_analysis(tmp_path, "analysis/id-1")
    assert build_artifact_summary(artifact) == build_artifact_summary(artifact)
    assert build_opportunity_rows(artifact) == build_opportunity_rows(artifact)
    summary = build_artifact_summary(artifact)
    assert summary.opportunity_count == 2
    assert summary.combination_count == 0
    assert summary.provenances == ("synthetic-contract",)


def test_theme_css_is_deterministic_accessible_and_decorative_only() -> None:
    first = theme.theme_css()
    assert first == theme.theme_css()
    assert "@media (prefers-reduced-motion: reduce)" in first
    assert "pointer-events: none" in first
    assert "animation: none !important" in first
    assert "18s" not in first
    source = inspect.getsource(theme)
    for forbidden in (
        "sports_analytics.artifacts",
        "sqlite",
        "os.environ",
        "load_settings",
        "expected_value",
        "model_probability",
    ):
        assert forbidden not in source


def test_streamlit_application_import_and_empty_state_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.helpers import repository_root

    app_path = repository_root() / "app.py"
    monkeypatch.chdir(tmp_path)
    app = AppTest.from_file(str(app_path))
    app.run(timeout=15)
    assert not app.exception
    assert any("Sports Analytics Local" in item.value for item in app.title)
    assert not any(
        "No valid typed analysis or backtest artifacts" in item.value for item in app.info
    )


def _two_leg_rules() -> CombinationRules:
    return CombinationRules(
        minimum_legs=2,
        maximum_legs=2,
        selection_minimum_odds=Decimal("1.01"),
        selection_maximum_odds=Decimal("100"),
        combined_minimum_odds=Decimal("1.01"),
        combined_maximum_odds=Decimal("1000"),
        allow_unknown_dependencies=False,
        allow_multiple_sports=True,
        allow_multiple_dates=True,
    )
