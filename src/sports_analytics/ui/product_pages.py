"""Read-only Streamlit renderer for persisted football product state."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from sports_analytics.artifacts import AnalyticalArtifact


def render_football_product(
    artifact: AnalyticalArtifact,
    *,
    exports_root: Path | None = None,
) -> None:
    """Render only already verified persisted read-model fields."""
    payload = artifact.payload
    assert isinstance(payload, dict)
    model = payload["model_status"]
    product = payload["product_state"]
    lineage = payload["artifact_lineage"]
    capabilities = payload["market_capabilities"]
    assert isinstance(model, dict)
    assert isinstance(product, dict)
    assert isinstance(lineage, dict)
    assert isinstance(capabilities, list)

    st.title("Football model & fair odds")
    st.caption(
        "Checksum-verified persisted state. This page does not train, scrape, "
        "evaluate, or place bets."
    )
    columns = st.columns(4)
    columns[0].metric("Product mode", _metric_value(product.get("mode"), "unavailable"))
    columns[1].metric("Placeable manual proposals", _metric_value(product.get("proposal_count"), 0))
    columns[2].metric(
        "Proposed accumulators",
        _metric_value(product.get("accumulator_count"), 0),
    )
    columns[3].metric(
        "Placement",
        _metric_value(product.get("placement_state"), "manual-only"),
    )

    st.subheader("Model status")
    st.json(model)
    operational_state = product.get("operational_state")
    if operational_state == "no-production-champion":
        st.warning(
            "Operational evidence hold: no registered active production champion. "
            "Probabilities, fair odds, opportunities, and proposals are unavailable."
        )
    elif operational_state == "economic-evidence-hold":
        st.warning(
            "Analytical opportunities are held from proposal publication because "
            "prospective and economic evidence requirements are not satisfied."
        )
    elif operational_state == "fair-odds-only":
        st.info(
            "A verified active champion produced fair odds, but no current offered "
            "quote exists. Opportunity analysis and proposals are unavailable."
        )
    elif product.get("mode") == "synthetic-contract-research-only":
        st.warning("Synthetic contract proof: research-only and not authorized for placement.")
    st.info(
        "Fair odds are model estimates. Offered odds are real imported external "
        "prices. EV and price-based proposals exist only when offered odds are present."
    )
    eligibility = product.get("eligibility")
    if isinstance(eligibility, dict):
        st.subheader("Production eligibility")
        st.json(eligibility)
    evidence = product.get("economic_evidence")
    if isinstance(evidence, dict):
        st.subheader("Economic evidence")
        st.warning(
            "The compatible historical market-only 1X2 baseline materially outperformed "
            "the score model. Historical closing prices remain diagnostic only."
        )
        st.json(evidence)

    st.subheader("Market coverage")
    st.dataframe(capabilities, hide_index=True, use_container_width=True)
    trusted_sports = trusted_sport_options(payload)
    persisted_policy = product.get("sport_policy")
    assert isinstance(persisted_policy, dict)
    allowed = persisted_policy.get("allowed_sports")
    mode = persisted_policy.get("mode")
    assert isinstance(allowed, list)
    st.subheader("Persisted proposal sport policy")
    st.multiselect(
        "Selected sports",
        trusted_sports,
        default=tuple(item for item in allowed if item in trusted_sports),
        disabled=True,
    )
    st.radio(
        "Sport combination mode",
        ("combine-selected-sports", "separate-by-sport"),
        index=0 if mode == "combine-selected-sports" else 1,
        disabled=True,
    )
    statuses = product.get("sport_statuses")
    if isinstance(statuses, list):
        st.dataframe(statuses, hide_index=True, use_container_width=True)
    _render_player_context(product.get("player_context"))
    del exports_root
    with st.expander("Immutable artifact lineage"):
        st.json(lineage)
    st.warning("Every proposal is informational. Final bookmaker placement remains manual.")


def _metric_value(value: object, fallback: str | int) -> str | int | float:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int | float | str):
        return value
    return fallback


def trusted_sport_options(payload: object) -> tuple[str, ...]:
    """Derive selector values only from persisted capability rows."""
    if not isinstance(payload, dict):
        return ()
    rows = payload.get("market_capabilities")
    if not isinstance(rows, list):
        return ()
    sports: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        sport = row.get("sport_code")
        if isinstance(sport, str) and sport:
            sports.add(sport)
    return tuple(sorted(sports))


def _render_player_context(value: object) -> None:
    st.subheader("Persisted player context")
    if not isinstance(value, dict):
        st.info("Player context unavailable. The active model did not consume player evidence.")
        return
    st.caption(
        "Displayed evidence is persisted only. Current-only observations remain display-only "
        "until historically equivalent pre-kickoff evidence exists."
    )
    st.json(value)
