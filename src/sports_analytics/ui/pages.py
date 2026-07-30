"""Streamlit page renderers for verified read-only analytical artifacts."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import streamlit as st

from sports_analytics.artifacts import TypedAnalyticalArtifact
from sports_analytics.combinations.contracts import (
    CombinationRules,
    validate_combination_manual,
)
from sports_analytics.services.combinations_trusted import (
    opportunities_from_typed_artifact,
)
from sports_analytics.ui.view_models import (
    ArtifactSummary,
    CombinationDisplayRow,
    OpportunityDisplayRow,
    OpportunityFilters,
    build_artifact_summary,
    build_backtest_display,
    build_combination_rows,
    build_opportunity_rows,
    collect_audit_identifiers,
    dataset_rows_for_display,
    filter_combinations_by_odds,
    filter_opportunities,
    format_odds,
    format_percent,
    short_identifier,
    unique_values,
)

PAGES: tuple[str, ...] = (
    "Data status",
    "Football product",
    "Opportunities",
    "Single detail",
    "Accumulator builder",
    "Backtesting",
    "Artifact & audit",
)


def render_page(page: str, artifact: TypedAnalyticalArtifact) -> None:
    """Render one selected page from an already verified artifact."""
    if page == "Data status":
        render_dashboard(artifact)
    elif page == "Opportunities":
        render_opportunities(artifact)
    elif page == "Single detail":
        render_single_detail(artifact)
    elif page == "Accumulator builder":
        render_accumulator_builder(artifact)
    elif page == "Backtesting":
        render_backtesting(artifact)
    elif page == "Artifact & audit":
        render_audit(artifact)
    else:
        st.error("Unknown page selection. Choose another page from the sidebar.")


def render_dashboard(artifact: TypedAnalyticalArtifact) -> None:
    """Render selected-artifact status and persisted lineage summaries."""
    summary = build_artifact_summary(artifact)
    st.title("Data status")
    st.caption("Read-only status for the selected, checksum-verified analytical artifact.")
    _render_data_warning(summary)

    columns = st.columns(5)
    metrics = (
        ("Opportunities", summary.opportunity_count),
        ("Eligible decisions", summary.eligible_count),
        ("Rejected decisions", summary.rejected_count),
        ("Combinations", summary.combination_count),
        ("Settlements", summary.settlement_count),
    )
    for column, (label, value) in zip(columns, metrics, strict=True):
        column.metric(label, value)

    st.subheader("Selected artifact")
    with st.container(border=True):
        left, middle, right = st.columns(3)
        left.markdown(f"**Kind**  \n{summary.artifact_kind}")
        middle.markdown(f"**Schema**  \n{summary.schema_version}")
        right.markdown(f"**Relative directory**  \n`{summary.relative_directory}`")
        st.code(summary.artifact_id, language=None)
        st.caption(f"Manifest checksum: {summary.checksum_sha256}")

    st.subheader("Dataset inventory")
    st.dataframe(
        [{"dataset": name, "rows": count} for name, count in summary.dataset_counts],
        hide_index=True,
        use_container_width=True,
    )

    lineage_left, lineage_right = st.columns(2)
    with lineage_left:
        st.subheader("Model lineage")
        _identifier_list(summary.model_artifact_ids, "No model artifact identifiers are present.")
    with lineage_right:
        st.subheader("Feature lineage")
        _identifier_list(
            summary.feature_artifact_ids,
            "No feature artifact identifiers are present.",
        )

    with st.expander("Current product limitations"):
        st.markdown(
            """
- This interface reads persisted analysis and backtest artifacts; it does not ingest,
  scrape, predict, train, or write analytical results.
- Betclic, Betano, live odds, upcoming-event providers, staking, and operational
  settlement are not implemented.
- Historical replay, closing-line benchmarks, and synthetic contract rows must not be
  interpreted as current betting opportunities.
"""
        )


def render_opportunities(artifact: TypedAnalyticalArtifact) -> None:
    """Render searchable, deterministic opportunity browsing."""
    st.title("Opportunities")
    st.caption(
        "Persisted analytical rows only. Closing-line historical prices are not "
        "currently executable odds."
    )
    rows = build_opportunity_rows(artifact)
    if not rows:
        st.info("The selected artifact contains no opportunity rows.")
        return

    filters = _opportunity_filter_controls(rows)
    filtered = filter_opportunities(rows, filters)
    top_left, top_right = st.columns([1, 3])
    top_left.metric("Matching rows", len(filtered))
    top_right.caption(
        "Eligibility and rejection labels are persisted decisions; filtering does not "
        "re-evaluate or alter them."
    )
    if not filtered:
        st.info("No persisted opportunities match the selected filters.")
        return
    st.dataframe(
        [row.table_row() for row in filtered],
        hide_index=True,
        use_container_width=True,
        height=560,
        column_config={
            "model_probability": st.column_config.NumberColumn(format="percent"),
            "raw_implied_probability": st.column_config.NumberColumn(format="percent"),
            "normalized_implied_probability": st.column_config.NumberColumn(format="percent"),
            "overround": st.column_config.NumberColumn(format="percent"),
            "edge": st.column_config.NumberColumn(format="percent"),
            "expected_value": st.column_config.NumberColumn(format="percent"),
            "decimal_odds": st.column_config.NumberColumn(format="%.2f"),
        },
    )


def render_single_detail(artifact: TypedAnalyticalArtifact) -> None:
    """Render one persisted opportunity with full identity and audit context."""
    st.title("Single detail")
    rows = build_opportunity_rows(artifact)
    if not rows:
        st.info("The selected artifact contains no opportunity rows to inspect.")
        return
    labels = {_opportunity_label(row): row for row in rows}
    selected_label = st.selectbox("Opportunity", tuple(labels))
    if selected_label is None:
        return
    row = labels[selected_label]

    summary = build_artifact_summary(artifact)
    _render_data_warning(summary)
    status_text = (
        f"Eligible · accepted rank {row.accepted_rank}"
        if row.decision_status == "eligible"
        else f"Rejected · {', '.join(row.rejection_codes) or 'no rejection code persisted'}"
    )
    st.info(status_text)

    metric_columns = st.columns(4)
    metric_columns[0].metric("Decimal odds", format_odds(row.decimal_odds))
    metric_columns[1].metric("Model probability", format_percent(row.model_probability))
    metric_columns[2].metric("Edge", format_percent(row.edge))
    metric_columns[3].metric("Expected value", format_percent(row.expected_value))

    identity, pricing = st.columns(2)
    with identity:
        st.subheader("Canonical identity")
        with st.container(border=True):
            st.markdown(f"**Event ID**  \n`{row.canonical_event_id}`")
            st.markdown(f"**Event start (UTC)**  \n{row.event_start_utc}")
            st.markdown(f"**Market**  \n`{row.market_key}`")
            st.markdown(f"**Outcome**  \n`{row.outcome}`")
            st.markdown(f"**Selection ID**  \n`{row.selection_id}`")
            st.markdown(f"**Participant ID**  \n`{row.canonical_participant_id}`")
    with pricing:
        st.subheader("Complete-market context")
        with st.container(border=True):
            st.markdown(f"**Provider / source**  \n{row.provider_id} / {row.source}")
            st.markdown(f"**Evaluation mode**  \n`{row.evaluation_mode}`")
            st.markdown(
                f"**Raw / normalized implied**  \n"
                f"{format_percent(row.raw_implied_probability)} / "
                f"{format_percent(row.normalized_implied_probability)}"
            )
            st.markdown(f"**Complete-market raw total**  \n{row.complete_market_raw_total:.4f}")
            st.markdown(f"**Overround**  \n{format_percent(row.overround)}")

    with st.expander("Canonical selection payload", expanded=True):
        st.json(row.selection)
    with st.expander("Quote, prediction, and timing audit"):
        st.json(
            {
                "opportunity_id": row.opportunity_id,
                "evaluation_id": row.evaluation_id,
                "prediction_id": row.prediction_id,
                "selection_id": row.selection_id,
                "provenance": row.provenance,
                "quote_series_id": row.quote_series_id,
                "quote_observation_id": row.quote_observation_id,
                "quoted_at_utc": row.quoted_at_utc,
                "source_observed_at_utc": row.source_observed_at_utc,
                "decision_as_of_utc": row.decision_as_of_utc,
                "prediction_quality": row.prediction_quality,
                "accepted_rank": row.accepted_rank,
                "rejection_codes": list(row.rejection_codes),
            }
        )
    with st.expander("Model, feature, and dependency lineage"):
        st.json(
            {
                "model_artifact_id": row.model_artifact_id,
                "model_checksum_sha256": row.model_checksum_sha256,
                "model_specification_version": row.model_specification_version,
                "feature_artifact_id": row.feature_artifact_id,
                "feature_manifest_checksum_sha256": (row.feature_manifest_checksum_sha256),
                "feature_specification_version": row.feature_specification_version,
                "feature_row_id": row.feature_row_id,
                "dependency_keys": list(row.dependency_keys),
                "participant_ids": list(row.participant_ids),
                "dependency_metadata_complete": row.dependency_metadata_complete,
                "dependency_metadata_provenance": row.dependency_metadata_provenance,
            }
        )


def render_accumulator_builder(artifact: TypedAnalyticalArtifact) -> None:
    """Render local manual validation and persisted automatic combinations."""
    st.title("Accumulator builder")
    st.caption(
        "Manual results are local interactive previews, not persisted artifacts or "
        "bookmaker-accepted bets."
    )
    manual_tab, automatic_tab = st.tabs(("Manual selection", "Persisted combinations"))
    with manual_tab:
        _render_manual_accumulator(artifact)
    with automatic_tab:
        _render_persisted_combinations(artifact)


def render_backtesting(artifact: TypedAnalyticalArtifact) -> None:
    """Render persisted backtest metrics and series without recomputation."""
    st.title("Backtesting")
    if artifact.artifact_kind != "backtest":
        st.info("Select a verified backtest artifact to view persisted backtest results.")
        return
    display = build_backtest_display(artifact)
    if not display.aggregate_metrics:
        st.info("The backtest artifact contains no aggregate metric row.")
        return
    aggregate = display.aggregate_metrics[0]
    st.caption(
        "All figures and chart points below are persisted in the selected artifact; "
        "the interface does not calculate replacement metrics."
    )

    first = st.columns(5)
    _metric(first[0], "Bets", aggregate.get("bet_count"))
    _metric(first[1], "Wins", aggregate.get("win_count"))
    _metric(first[2], "Losses", aggregate.get("loss_count"))
    _metric(first[3], "ROI", _percent_value(aggregate.get("roi")))
    _metric(first[4], "Hit rate", _percent_value(aggregate.get("hit_rate")))
    second = st.columns(5)
    _metric(second[0], "Candidates", aggregate.get("candidate_count"))
    _metric(second[1], "Rejected", aggregate.get("rejection_count"))
    _metric(second[2], "Accepted singles", aggregate.get("accepted_single_count"))
    _metric(
        second[3],
        "Accepted combinations",
        aggregate.get("accepted_combination_count"),
    )
    _metric(second[4], "Quote coverage", _percent_value(aggregate.get("quote_coverage")))
    third = st.columns(5)
    _metric(third[0], "Staked units", aggregate.get("staked_units"))
    _metric(third[1], "Returned units", aggregate.get("returned_units"))
    _metric(third[2], "Net profit units", aggregate.get("net_profit_units"))
    _metric(third[3], "Average odds", aggregate.get("average_decimal_odds"))
    _metric(third[4], "Max drawdown", aggregate.get("maximum_drawdown_units"))
    fourth = st.columns(5)
    _metric(fourth[0], "Pushes", aggregate.get("push_count"))
    _metric(fourth[1], "Voids", aggregate.get("void_count"))
    _metric(fourth[2], "Test events", aggregate.get("test_event_count"))
    _metric(
        fourth[3],
        "Complete quote events",
        aggregate.get("complete_quote_event_count"),
    )
    _metric(fourth[4], "Gross return units", aggregate.get("gross_return_units"))

    st.subheader("Cumulative profit units")
    if display.cumulative_profit_units:
        st.line_chart(
            {"cumulative_profit_units": display.cumulative_profit_units},
            use_container_width=True,
        )
    else:
        st.info("No cumulative-profit series is persisted in this artifact.")

    with st.expander("Strategy, mode, coverage, and aggregate audit", expanded=True):
        st.json(aggregate)
    st.subheader("Fold metrics")
    if display.fold_metrics:
        st.dataframe(display.fold_metrics, hide_index=True, use_container_width=True)
    else:
        st.info("No fold metric rows are persisted.")
    st.subheader("Settlements")
    if display.settlements:
        st.dataframe(display.settlements, hide_index=True, use_container_width=True)
    else:
        st.info("No settlement rows are persisted.")
    st.subheader("Persisted combinations")
    _combination_table(display.combinations)


def render_audit(artifact: TypedAnalyticalArtifact) -> None:
    """Render read-only artifact identity, datasets, lineage, and raw rows."""
    st.title("Artifact & audit")
    st.caption(
        "Only relative artifact paths and already verified typed datasets are shown. "
        "No environment values or private absolute paths are exposed."
    )
    st.json(
        {
            "artifact_id": artifact.artifact_id,
            "artifact_kind": artifact.artifact_kind,
            "schema_version": artifact.schema_version,
            "relative_directory": artifact.relative_directory,
            "manifest_checksum_sha256": artifact.checksum_sha256,
        }
    )
    inventory = [
        {
            "dataset": dataset.name,
            "schema_version": dataset.schema_version,
            "rows": dataset.row_count,
            "id_field": dataset.id_field,
            "dataset_checksum_sha256": dataset.checksum_sha256,
        }
        for dataset in artifact.datasets
    ]
    st.subheader("Verified dataset inventory")
    st.dataframe(inventory, hide_index=True, use_container_width=True)

    st.subheader("Persisted identifiers")
    for dataset_name, identifiers in collect_audit_identifiers(artifact):
        with st.expander(f"{dataset_name} · {len(identifiers)} identifiers"):
            if identifiers:
                st.code("\n".join(identifiers), language=None)
            else:
                st.caption("No identifiers are present.")

    st.subheader("Validated raw rows")
    st.caption(
        "These optional views come from the selected in-memory typed artifact after "
        "full validation."
    )
    for dataset in artifact.datasets:
        with st.expander(f"{dataset.name} · {dataset.row_count} rows"):
            rows = dataset_rows_for_display(artifact, dataset.name)
            if rows:
                st.dataframe(rows, hide_index=True, use_container_width=True)
            else:
                st.caption("This verified dataset is empty.")


def _opportunity_filter_controls(
    rows: tuple[OpportunityDisplayRow, ...],
) -> OpportunityFilters:
    with st.expander("Filters", expanded=True):
        search = st.text_input(
            "Search",
            placeholder="Event, market, provider, outcome, ID, or rejection code",
        )
        first = st.columns(4)
        sports = tuple(first[0].multiselect("Sport", unique_values(rows, "sport")))
        market_families = tuple(
            first[1].multiselect("Market family", unique_values(rows, "market_family"))
        )
        market_keys = tuple(first[2].multiselect("Market key", unique_values(rows, "market_key")))
        market_periods = tuple(
            first[3].multiselect("Market period", unique_values(rows, "market_period"))
        )
        second = st.columns(4)
        participant_scopes = tuple(
            second[0].multiselect(
                "Participant scope",
                unique_values(rows, "participant_scope"),
            )
        )
        sources = tuple(second[1].multiselect("Source", unique_values(rows, "source")))
        providers = tuple(second[2].multiselect("Provider", unique_values(rows, "provider_id")))
        evaluation_modes = tuple(
            second[3].multiselect(
                "Evaluation mode",
                unique_values(rows, "evaluation_mode"),
            )
        )
        third = st.columns(4)
        decision_status = third[0].selectbox("Decision status", ("all", "eligible", "rejected"))
        prediction_quality = third[1].selectbox("Prediction quality", ("all", "passed", "failed"))
        dates = [_event_date(row) for row in rows]
        event_date_from = third[2].date_input(
            "Event date from",
            value=min(dates),
            min_value=min(dates),
            max_value=max(dates),
        )
        event_date_to = third[3].date_input(
            "Event date to",
            value=max(dates),
            min_value=min(dates),
            max_value=max(dates),
        )
        odds = [row.decimal_odds for row in rows]
        fourth = st.columns(5)
        minimum_decimal_odds = fourth[0].number_input(
            "Minimum decimal odds",
            min_value=1.0001,
            value=float(min(odds)),
            step=0.05,
        )
        maximum_decimal_odds = fourth[1].number_input(
            "Maximum decimal odds",
            min_value=1.0001,
            value=float(max(odds)),
            step=0.05,
        )
        minimum_model_probability = fourth[2].number_input(
            "Minimum model probability",
            min_value=0.0,
            max_value=1.0,
            value=0.0,
            step=0.01,
        )
        minimum_edge = fourth[3].number_input(
            "Minimum edge",
            value=float(min(row.edge for row in rows)),
            step=0.01,
        )
        minimum_expected_value = fourth[4].number_input(
            "Minimum expected value",
            value=float(min(row.expected_value for row in rows)),
            step=0.01,
        )
    return OpportunityFilters(
        search=search,
        sports=sports,
        market_families=market_families,
        market_keys=market_keys,
        market_periods=market_periods,
        participant_scopes=participant_scopes,
        sources=sources,
        providers=providers,
        evaluation_modes=evaluation_modes,
        event_date_from=event_date_from if isinstance(event_date_from, date) else None,
        event_date_to=event_date_to if isinstance(event_date_to, date) else None,
        decision_status=decision_status,
        prediction_quality=prediction_quality,
        minimum_decimal_odds=minimum_decimal_odds,
        maximum_decimal_odds=maximum_decimal_odds,
        minimum_model_probability=minimum_model_probability,
        minimum_edge=minimum_edge,
        minimum_expected_value=minimum_expected_value,
    )


def _render_manual_accumulator(artifact: TypedAnalyticalArtifact) -> None:
    eligible_only = st.checkbox("Eligible opportunities only", value=True)
    domain_opportunities = opportunities_from_typed_artifact(
        artifact,
        eligible_only=eligible_only,
    )
    if not domain_opportunities:
        st.info("No opportunities are available under the selected persisted decision scope.")
        return

    bounds = st.columns(4)
    leg_minimum = bounds[0].number_input(
        "Minimum odds per leg", min_value=1.0001, value=1.01, step=0.05
    )
    leg_maximum = bounds[1].number_input(
        "Maximum odds per leg", min_value=1.0001, value=100000.0, step=0.05
    )
    total_minimum = bounds[2].number_input(
        "Minimum total odds", min_value=1.0001, value=1.01, step=0.05
    )
    total_maximum = bounds[3].number_input(
        "Maximum total odds", min_value=1.0001, value=1000000.0, step=0.05
    )
    candidates = tuple(
        item
        for item in domain_opportunities
        if Decimal(str(leg_minimum)) <= item.decimal_odds <= Decimal(str(leg_maximum))
    )
    display_by_id = {row.opportunity_id: row for row in build_opportunity_rows(artifact)}
    options = tuple(item.opportunity_id for item in candidates)
    selected_ids = st.multiselect(
        "Legs",
        options,
        format_func=lambda opportunity_id: _opportunity_label(display_by_id[opportunity_id]),
    )
    if len(selected_ids) < 2:
        st.info("Select at least two legs to create a local interactive preview.")
        return
    selected = tuple(item for item in candidates if item.opportunity_id in selected_ids)
    for index, leg in enumerate(selected, start=1):
        display = display_by_id[leg.opportunity_id]
        with st.container(border=True):
            st.markdown(f"**Leg {index} · {display.sport} · {display.market_key}**")
            st.caption(
                f"{display.canonical_event_id} · {display.event_start_utc} · "
                f"{display.outcome} · {format_odds(display.decimal_odds)}"
            )
    if leg_maximum < leg_minimum or total_maximum < total_minimum:
        st.error("Odds bounds must be ordered from minimum to maximum.")
        return
    rules = CombinationRules(
        minimum_legs=len(selected),
        maximum_legs=len(selected),
        selection_minimum_odds=Decimal(str(leg_minimum)),
        selection_maximum_odds=Decimal(str(leg_maximum)),
        combined_minimum_odds=Decimal(str(total_minimum)),
        combined_maximum_odds=Decimal(str(total_maximum)),
        allow_unknown_dependencies=False,
        maximum_candidates=max(50, len(selected)),
        maximum_outputs=1,
        allow_multiple_sports=True,
        allow_multiple_dates=True,
    )
    preview = validate_combination_manual(selected, rules=rules)
    if not preview.eligible or preview.combination is None:
        st.error("This manual preview is invalid.")
        for reason in preview.rejection_reasons:
            st.markdown(f"- {reason}")
    else:
        combination = preview.combination
        st.success("Valid structurally separate local interactive preview.")
        metrics = st.columns(4)
        metrics[0].metric("Legs", combination.leg_count)
        metrics[1].metric("Total odds", format_odds(float(combination.total_decimal_odds)))
        metrics[2].metric("Joint probability", format_percent(combination.joint_probability))
        metrics[3].metric("Expected value", format_percent(combination.expected_value))
        st.warning(combination.structural_independence_warning)
    with st.expander("Dependency assessment"):
        st.dataframe(
            [
                {
                    "left_opportunity_id": item.left_opportunity_id,
                    "right_opportunity_id": item.right_opportunity_id,
                    "classification": item.classification.value,
                    "reason": item.reason,
                }
                for item in preview.dependencies
            ],
            hide_index=True,
            use_container_width=True,
        )


def _render_persisted_combinations(artifact: TypedAnalyticalArtifact) -> None:
    combinations = build_combination_rows(artifact)
    if not combinations:
        st.info("The selected artifact contains no persisted combinations.")
        return
    bounds = st.columns(4)
    leg_minimum = bounds[0].number_input(
        "Persisted: minimum odds per leg",
        min_value=1.0001,
        value=1.01,
        step=0.05,
    )
    leg_maximum = bounds[1].number_input(
        "Persisted: maximum odds per leg",
        min_value=1.0001,
        value=100000.0,
        step=0.05,
    )
    total_minimum = bounds[2].number_input(
        "Persisted: minimum total odds",
        min_value=1.0001,
        value=1.01,
        step=0.05,
    )
    total_maximum = bounds[3].number_input(
        "Persisted: maximum total odds",
        min_value=1.0001,
        value=1000000.0,
        step=0.05,
    )
    filtered = filter_combinations_by_odds(
        combinations,
        leg_minimum_odds=leg_minimum,
        leg_maximum_odds=leg_maximum,
        total_minimum_odds=total_minimum,
        total_maximum_odds=total_maximum,
    )
    _combination_table(filtered)
    for combination in filtered:
        with st.expander(
            f"{short_identifier(combination.combination_id)} · {len(combination.legs)} legs"
        ):
            st.json(
                {
                    "combination_id": combination.combination_id,
                    "policy_id": combination.policy_id,
                    "policy_version": combination.policy_version,
                    "common_decision_time_utc": combination.common_decision_time_utc,
                    "earliest_event_start_utc": combination.earliest_event_start_utc,
                    "latest_event_start_utc": combination.latest_event_start_utc,
                    "total_decimal_odds": combination.total_decimal_odds,
                    "joint_probability": combination.joint_probability,
                    "expected_value": combination.expected_value,
                    "eligible": combination.eligible,
                    "rejection_reasons": list(combination.rejection_reasons),
                    "dependencies": list(combination.dependencies),
                }
            )
            for index, leg in enumerate(combination.legs, start=1):
                with st.container(border=True):
                    st.markdown(f"**Leg {index} · {leg.sport} · {leg.market_key}**")
                    st.caption(
                        f"{leg.canonical_event_id} · {leg.event_start_utc} · "
                        f"{leg.outcome} · {format_odds(leg.decimal_odds)}"
                    )


def _combination_table(rows: tuple[CombinationDisplayRow, ...]) -> None:
    if rows:
        st.dataframe(
            [row.table_row() for row in rows],
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.info("No persisted combinations match the selected odds bounds.")


def _render_data_warning(summary: ArtifactSummary) -> None:
    warning = summary.warning
    st.warning(f"**{warning.title}.** {warning.message}")


def _identifier_list(values: tuple[str, ...], empty_message: str) -> None:
    if not values:
        st.info(empty_message)
        return
    for value in values:
        with st.container(border=True):
            st.code(value, language=None)


def _opportunity_label(row: OpportunityDisplayRow) -> str:
    return (
        f"{row.sport} · {row.event_start_utc} · {row.market_key} · {row.outcome} · "
        f"{format_odds(row.decimal_odds)} · {short_identifier(row.opportunity_id)}"
    )


def _event_date(row: OpportunityDisplayRow) -> date:
    return date.fromisoformat(row.event_start_utc[:10])


def _metric(container: Any, label: str, value: object) -> None:
    container.metric(label, "—" if value is None else value)


def _percent_value(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "—"
    return format_percent(float(value))
