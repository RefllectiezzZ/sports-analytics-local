"""Read-only Streamlit renderer for persisted football product state."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from sports_analytics.artifacts import AnalyticalArtifact
from sports_analytics.policies.proposal import (
    PublishedProposalPolicy,
    publish_proposal_policy,
)
from sports_analytics.proposals.football import SportCombinationMode


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
    columns[1].metric("Proposed singles", _metric_value(product.get("proposal_count"), 0))
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
    st.info(
        "Fair odds are model estimates. Offered odds are real imported external "
        "prices. EV and price-based proposals exist only when offered odds are present."
    )

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
    if exports_root is not None:
        _render_policy_publisher(
            exports_root=exports_root,
            trusted_sports=trusted_sports,
            persisted_allowed=tuple(str(item) for item in allowed),
            persisted_mode=str(mode),
        )
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


def _render_policy_publisher(
    *,
    exports_root: Path,
    trusted_sports: tuple[str, ...],
    persisted_allowed: tuple[str, ...],
    persisted_mode: str,
) -> None:
    st.subheader("Publish proposal policy")
    with st.form("publish-proposal-policy"):
        selected = st.multiselect(
            "Allowed sports",
            trusted_sports,
            default=tuple(item for item in persisted_allowed if item in trusted_sports),
        )
        mode = st.radio(
            "Combination mode",
            ("combine-selected-sports", "separate-by-sport"),
            index=0 if persisted_mode == "combine-selected-sports" else 1,
        )
        providers = st.text_input(
            "Allowed providers (comma separated; empty means any registered provider)",
            "",
        )
        minimum_legs = st.number_input("Minimum legs", min_value=1, max_value=20, value=2)
        maximum_legs = st.number_input("Maximum legs", min_value=1, max_value=20, value=4)
        minimum_total_odds = st.number_input("Minimum total odds", min_value=1.01, value=1.20)
        maximum_total_odds = st.number_input("Maximum total odds", min_value=1.01, value=100.0)
        minimum_edge = st.number_input("Minimum edge", min_value=0.0, max_value=1.0, value=0.02)
        minimum_ev = st.number_input(
            "Minimum expected value", min_value=0.0, max_value=1.0, value=0.03
        )
        maximum_uncertainty = st.number_input(
            "Maximum uncertainty", min_value=0.0, max_value=1.0, value=0.10
        )
        publish = st.form_submit_button("Publish immutable policy")
    if not publish:
        return
    try:
        policy = PublishedProposalPolicy(
            allowed_sports=tuple(sorted(set(selected))),
            combination_mode=SportCombinationMode(mode),
            provider_policy=tuple(
                sorted({item.strip() for item in providers.split(",") if item.strip()})
            ),
            minimum_legs=int(minimum_legs),
            maximum_legs=int(maximum_legs),
            minimum_total_odds=float(minimum_total_odds),
            maximum_total_odds=float(maximum_total_odds),
            minimum_edge=float(minimum_edge),
            minimum_expected_value=float(minimum_ev),
            maximum_uncertainty=float(maximum_uncertainty),
        )
        artifact = publish_proposal_policy(
            root=exports_root,
            relative_directory=f"proposal-policies/{policy.configuration_id}",
            policy=policy,
        )
    except Exception as exc:  # noqa: BLE001 - Streamlit boundary reports safe domain text.
        st.error(f"Policy was not published: {exc}")
        return
    st.success(f"Published immutable policy {artifact.artifact_id[:12]}")
