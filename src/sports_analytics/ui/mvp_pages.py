"""Professional Streamlit pages for the operator-first local MVP."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import streamlit as st

from sports_analytics.bookmakers.operator_quotes import (
    parse_operator_quote_csv,
    parse_operator_quote_json,
)
from sports_analytics.core.exceptions import ConfigurationError, SportsAnalyticsError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.mvp.operator_inputs import (
    MATCH_INPUT_FIELDS,
    ODDS_INPUT_FIELDS,
    SUPPORTED_MANUAL_MARKETS,
    RowIssue,
    parse_human_match_upload,
)
from sports_analytics.mvp.orchestrator import MVPOrchestrator
from sports_analytics.mvp.state import MVPState, MVPStatus

MVP_PAGES: Final[tuple[str, ...]] = (
    "Dashboard",
    "Bets",
    "Matches",
    "Odds",
    "History",
    "System",
)


def render_mvp_page(
    page: str,
    *,
    orchestrator: MVPOrchestrator,
    exports_root: Path,
) -> None:
    """Render one fixed MVP navigation destination."""
    if page == "Dashboard":
        render_dashboard(orchestrator)
    elif page == "Bets":
        render_bets(orchestrator)
    elif page == "Matches":
        render_matches(orchestrator)
    elif page == "Odds":
        render_odds(orchestrator)
    elif page == "History":
        render_history(orchestrator)
    elif page == "System":
        render_system(orchestrator, exports_root=exports_root)
    else:
        st.error("Unknown workspace page.")


def render_dashboard(orchestrator: MVPOrchestrator) -> None:
    """Render the bounded auto-refreshing persisted operational summary."""
    st.title("Sports Analytics Local")
    st.caption(
        "Operator workspace · localhost only · analytical generation is automatic · "
        "bookmaker placement remains manual"
    )

    @st.fragment(run_every="5s")
    def _persisted_status() -> None:
        status = orchestrator.inspect()
        _state_banner(status)
        cards = st.columns(5)
        cards[0].metric("Matches analysed", status.matches_analysed)
        cards[1].metric(
            "Awaiting odds",
            status.upcoming_event_count if status.current_quote_count == 0 else 0,
        )
        cards[2].metric("Analytical candidates", status.analytical_candidate_count)
        cards[3].metric("Held candidates", status.held_candidate_count)
        cards[4].metric(
            "Placeable manual proposals",
            status.placeable_manual_proposal_count,
        )
        left, right = st.columns((3, 2))
        with left:
            st.subheader("Operational setup")
            for label, state in status.steps:
                icon = {
                    "ready": "✓",
                    "action required": "→",
                    "running": "↻",
                    "failed": "!",
                    "optional/held": "○",
                }[state.value]
                st.markdown(f"**{icon} {label}**  \n{state.value}")
        with right:
            st.subheader("Current scope")
            st.markdown(
                f"**Competition**  \n{', '.join(status.active_competitions) or 'Not active'}"
            )
            st.markdown(
                f"**Production model**  \n"
                f"{', '.join(_short(item) for item in status.active_models) or 'Not active'}"
            )
            st.markdown(
                f"**Last successful analysis**  \n"
                f"{status.last_successful_analysis or 'Not available'}"
            )
            st.markdown(f"**Worker state**  \n{status.worker_state}")
            st.info(status.next_required_action)
        if status.blockers:
            with st.expander("Exact blockers", expanded=status.state != MVPState.FAILED):
                for blocker in status.blockers:
                    st.markdown(f"- {blocker}")

    _persisted_status()
    status = orchestrator.inspect()
    if status.state in {
        MVPState.HISTORICAL_DATA_REQUIRED,
        MVPState.MODEL_PREPARATION_REQUIRED,
    }:
        st.subheader("Guided first-run preparation")
        st.caption(
            "Preparation reuses only verified local historical snapshots and existing "
            "governance evidence. It does not create synthetic production evidence."
        )
        confirmed = st.checkbox(
            "I understand preparation may take time.",
            key="mvp_prepare_confirmation",
        )
        if st.button(
            "Prepare system",
            type="primary",
            disabled=not confirmed,
            key="mvp_prepare",
        ):
            with st.status("Preparing verified local state…", expanded=True):
                result = orchestrator.prepare_system()
            for action in result.actions:
                st.success(action)
            for blocker in result.blockers:
                st.warning(blocker)
            st.rerun()
        if status.state == MVPState.HISTORICAL_DATA_REQUIRED:
            st.info(
                "No verified local history is available. Use the existing allowlisted "
                "Football-Data ingestion path from System; downloads never run silently "
                "on launch or refresh."
            )
            competition = st.selectbox(
                "Historical competition",
                ("eng-premier-league", "prt-primeira-liga"),
                key="mvp_history_competition",
            )
            season = st.text_input(
                "Historical season",
                value="2024-2025",
                help="Canonical cross-year season, for example 2024-2025.",
                key="mvp_history_season",
            )
            if st.button(
                "Import allowlisted Football-Data history",
                key="mvp_enqueue_history",
            ):
                try:
                    job_id = orchestrator.enqueue_historical_data(
                        competition=competition,
                        season=season,
                    )
                    st.success(f"Historical preparation queued · {_short(job_id)}")
                except (SportsAnalyticsError, OSError, ValueError) as exc:
                    st.error(_safe(exc))
    st.warning(
        "Model probabilities, fair odds, edge, and EV are analytical estimates. "
        "Economic holds are expected until prospective evidence passes every gate."
    )


def render_matches(orchestrator: MVPOrchestrator) -> None:
    """Render registry-backed match upload and manual editing."""
    st.title("Matches")
    st.caption(
        "Add scheduled matches using verified team names. Canonical identities and "
        "artifact lineage are derived automatically."
    )
    try:
        choices = orchestrator.participant_choices()
    except (SportsAnalyticsError, OSError, ValueError) as exc:
        st.warning(f"Prepare the system before adding matches: {_safe(exc)}")
        return
    competitions = tuple(choices)
    all_teams = tuple(sorted({team for teams in choices.values() for team in teams}))
    upload = st.file_uploader(
        "Upload matches",
        type=("csv", "json"),
        key="mvp_match_upload",
        help="Human-friendly CSV or JSON; no UUIDs or checksums are required.",
    )
    if upload is not None:
        try:
            st.session_state["mvp_match_rows"] = list(
                parse_human_match_upload(upload.getvalue(), filename=upload.name)
            )
        except ConfigurationError as exc:
            st.error(_safe(exc))
    default_competition = competitions[0] if competitions else ""
    default_team = all_teams[0] if all_teams else ""
    rows = st.session_state.get(
        "mvp_match_rows",
        [
            {
                "competition": default_competition,
                "home_team": default_team,
                "away_team": default_team,
                "scheduled_time": "",
                "external_source_label": "",
            }
        ],
    )
    edited = st.data_editor(
        rows,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=False,
        column_config={
            "competition": st.column_config.SelectboxColumn(
                "Competition", options=competitions, required=True
            ),
            "home_team": st.column_config.SelectboxColumn(
                "Home team", options=all_teams, required=True
            ),
            "away_team": st.column_config.SelectboxColumn(
                "Away team", options=all_teams, required=True
            ),
            "scheduled_time": st.column_config.TextColumn(
                "Scheduled time (UTC or timezone-aware)", required=True
            ),
            "external_source_label": st.column_config.TextColumn(
                "External source label (optional)"
            ),
        },
        key="mvp_match_editor",
    )
    st.caption(
        "Competition-scoped registry validation is repeated on save, including event "
        "time and participant reconciliation checks."
    )
    if st.button("Save upcoming matches", type="primary", key="mvp_save_matches"):
        normalized = _records(edited, MATCH_INPUT_FIELDS)
        signature = _signature(normalized)
        if st.session_state.get("mvp_last_match_submission") == signature:
            st.info("This exact match submission was already processed.")
            return
        result = orchestrator.save_matches(normalized)
        _issues(result.validation.issues)
        if result.failure:
            st.error(result.failure)
        elif result.artifact_ids:
            st.session_state["mvp_last_match_submission"] = signature
            st.success(f"Published {len(result.artifact_ids)} verified upcoming-match artifact(s).")
            if result.analysis_artifact_ids:
                st.success("Automatic fair-odds analysis completed.")


def render_odds(orchestrator: MVPOrchestrator) -> None:
    """Render strict current-offered-odds upload and manual editing."""
    st.title("Odds")
    st.caption(
        "Enter real offered decimal odds. URLs, headers, cookies, tokens, selectors, "
        "scripts, and bookmaker login data are not accepted."
    )
    try:
        options = orchestrator.match_options()
    except (SportsAnalyticsError, OSError, ValueError) as exc:
        st.warning(f"Add verified upcoming matches first: {_safe(exc)}")
        return
    if not options:
        st.info("No future verified match is available. Add a match before prices.")
        return
    labels = tuple(item.label for item in options)
    providers = ("betano-pt", "betclic-pt")
    now_text = format_utc_timestamp(datetime.now(tz=UTC))
    upload = st.file_uploader(
        "Upload canonical odds",
        type=("csv", "json"),
        key="mvp_odds_upload",
    )
    if upload is not None:
        try:
            parsed = (
                parse_operator_quote_csv(upload.getvalue())
                if upload.name.lower().endswith(".csv")
                else parse_operator_quote_json(upload.getvalue())
            )
            label_by_id = {item.canonical_event_id: item.label for item in options}
            st.session_state["mvp_odds_rows"] = [
                {
                    "provider": item.provider_id,
                    "match": label_by_id.get(item.canonical_event_id, item.canonical_event_id),
                    "market": item.market_family,
                    "outcome": item.outcome_key,
                    "line": "" if item.line_value is None else str(item.line_value),
                    "decimal_odds": str(item.offered_decimal_odds),
                    "observed_timestamp": format_utc_timestamp(item.observed_at_utc),
                }
                for item in parsed
            ]
            st.success(f"Loaded {len(parsed)} canonical offered-price row(s).")
        except (SportsAnalyticsError, ValueError) as exc:
            st.error(_safe(exc))
    rows = st.session_state.get(
        "mvp_odds_rows",
        [
            {
                "provider": providers[0],
                "match": labels[0],
                "market": "match-result",
                "outcome": outcome,
                "line": "",
                "decimal_odds": "",
                "observed_timestamp": now_text,
            }
            for outcome in ("home", "draw", "away")
        ],
    )
    edited = st.data_editor(
        rows,
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "provider": st.column_config.SelectboxColumn(
                "Registered provider", options=providers, required=True
            ),
            "match": st.column_config.SelectboxColumn("Match", options=labels, required=True),
            "market": st.column_config.SelectboxColumn(
                "Market", options=SUPPORTED_MANUAL_MARKETS, required=True
            ),
            "outcome": st.column_config.TextColumn("Outcome", required=True),
            "line": st.column_config.TextColumn("Line (when applicable)"),
            "decimal_odds": st.column_config.TextColumn("Decimal odds", required=True),
            "observed_timestamp": st.column_config.TextColumn(
                "Observed timestamp (UTC)", required=True
            ),
        },
        key="mvp_odds_editor",
    )
    st.caption(f"Current UTC reference: {now_text}")
    if st.button("Save odds and analyse", type="primary", key="mvp_save_odds"):
        normalized = _records(edited, ODDS_INPUT_FIELDS)
        signature = _signature(normalized)
        if st.session_state.get("mvp_last_odds_submission") == signature:
            st.info("This exact odds submission was already processed.")
            return
        with st.status("Validating prices and refreshing the product…", expanded=True):
            result = orchestrator.save_odds(normalized)
        _issues(result.validation.issues)
        if result.failure:
            st.error(result.failure)
        elif result.product_artifact_ids:
            st.session_state["mvp_last_odds_submission"] = signature
            st.success("Validated real offered odds and refreshed persisted candidates.")


def render_bets(orchestrator: MVPOrchestrator) -> None:
    """Render truthful persisted singles and accumulators."""
    st.title("Bets")
    st.caption(
        "Automatically generated analytical candidates. Final bookmaker placement "
        "is always a separate manual action."
    )
    proposal = orchestrator.latest_proposals()
    if proposal is None or not isinstance(proposal.payload, dict):
        st.info("No persisted candidate set is available yet.")
        return
    singles = [
        cast(dict[str, object], item)
        for item in _list(proposal.payload.get("singles"))
        if isinstance(item, dict)
    ]
    try:
        match_labels = {
            item.canonical_event_id: item.label for item in orchestrator.match_options()
        }
    except (SportsAnalyticsError, OSError, ValueError):
        match_labels = {}
    singles_by_id = {
        str(item.get("decision_id")): item for item in singles if item.get("decision_id")
    }
    groups = (
        ("Ready for manual placement", lambda row: row.get("accepted") is True),
        (
            "Analytical candidates",
            lambda row: (
                row.get("offered_decimal_odds") is not None and not _list(row.get("reason_codes"))
            ),
        ),
        (
            "Held",
            lambda row: (
                row.get("offered_decimal_odds") is not None and bool(_list(row.get("reason_codes")))
            ),
        ),
        ("Rejected", lambda row: row.get("offered_decimal_odds") is None),
    )
    for title, predicate in groups:
        selected = [row for row in singles if predicate(row)]
        st.subheader(title)
        if not selected:
            st.caption("No rows in this status.")
            continue
        st.dataframe(
            [_single_display(row, match_labels=match_labels) for row in selected],
            hide_index=True,
            use_container_width=True,
            column_config={
                "model probability": st.column_config.NumberColumn(format="%.2f%%"),
                "edge": st.column_config.NumberColumn(format="%.2f%%"),
                "EV": st.column_config.NumberColumn(format="%.2f%%"),
            },
        )
    st.subheader("Accumulators")
    accumulators = [
        cast(dict[str, object], item)
        for item in _list(proposal.payload.get("accumulators"))
        if isinstance(item, dict)
    ]
    if not accumulators:
        st.caption("No eligible same-provider, separate-event accumulator candidate is persisted.")
    for row in accumulators:
        leg_labels = []
        for decision_id in _list(row.get("decision_ids")):
            single = singles_by_id.get(str(decision_id))
            if single is None:
                leg_labels.append(str(decision_id))
                continue
            event_id = str(single.get("canonical_event_id", ""))
            leg_labels.append(
                f"{match_labels.get(event_id, event_id)} · "
                f"{single.get('market_family', '')} · {single.get('outcome_key', '')}"
            )
        with st.container(border=True):
            st.markdown(
                f"**{row.get('provider_id', 'Provider unavailable')} · "
                f"{row.get('total_offered_odds', '—')} offered odd**"
            )
            st.caption(f"Legs: {' | '.join(leg_labels)}")
            metrics = st.columns(3)
            metrics[0].metric("Model probability", _percent(row.get("joint_probability")))
            metrics[1].metric("EV", _percent(row.get("expected_value")))
            metrics[2].metric("Dependency", str(row.get("dependency_state", "unavailable")))
            st.caption(
                "Same-provider proof: "
                f"{'verified' if row.get('same_bookmaker_confirmed') is True else 'held'} · "
                f"{row.get('placement_state', 'manual-only')}"
            )


def render_history(orchestrator: MVPOrchestrator) -> None:
    """Render a compact persisted operational history summary."""
    st.title("History")
    status = orchestrator.inspect()
    st.caption("Verified persisted operational history; no live bookmaker state.")
    columns = st.columns(4)
    columns[0].metric("Historical snapshots", status.historical_snapshot_count)
    columns[1].metric("Upcoming matches", status.upcoming_event_count)
    columns[2].metric("Current quote rows", status.current_quote_count)
    columns[3].metric("Accumulator candidates", status.accumulator_count)
    st.markdown(f"**Latest analysis**  \n{status.last_successful_analysis or 'Not available'}")
    st.markdown(f"**Current workflow state**  \n{status.state.value}")


def render_system(orchestrator: MVPOrchestrator, *, exports_root: Path) -> None:
    """Render system state and explicitly advanced audit data."""
    st.title("System")
    status = orchestrator.inspect()
    _state_banner(status)
    st.subheader("Runtime")
    st.write(
        {
            "worker": status.worker_state,
            "historical snapshots": status.historical_snapshot_count,
            "verified exports root": str(exports_root),
            "bookmaker placement": "manual-only",
            "bookmaker network required": False,
        }
    )
    st.subheader("Allowlisted historical ingestion")
    st.info(
        "Football-Data ingestion remains the only historical download path. "
        "It is explicit and never starts on launch or periodic refresh."
    )
    with st.expander("Advanced immutable artifact audit"):
        product = orchestrator.latest_product()
        if product is None:
            st.caption("No verified product read model is available.")
        else:
            st.code(product.relative_directory, language=None)
            st.code(product.artifact_id, language=None)
            st.caption(f"Manifest checksum: {product.checksum_sha256}")
            st.json(product.payload)
    st.warning(
        "There are no controls for model selection, forced promotion, economic "
        "overrides, arbitrary commands, bookmaker login, or bet placement."
    )


def _state_banner(status: MVPStatus) -> None:
    label = status.state.value.replace("-", " ").title()
    if status.state == MVPState.FAILED:
        st.error(f"{label}: {status.failure or 'operation failed closed'}")
    elif status.state in {
        MVPState.CANDIDATES_AVAILABLE,
        MVPState.PLACEABLE_MANUAL_PROPOSALS_AVAILABLE,
    }:
        st.success(label)
    elif status.state == MVPState.ANALYSING:
        st.info(label)
    else:
        st.warning(label)


def _issues(issues: tuple[RowIssue, ...]) -> None:
    for issue in issues:
        prefix = "Submission" if issue.row_number == 0 else f"Row {issue.row_number}"
        st.error(f"{prefix} · {issue.field}: {issue.message}")


def _records(value: Any, fields: tuple[str, ...]) -> tuple[dict[str, object], ...]:
    if hasattr(value, "to_dict"):
        raw = value.to_dict(orient="records")
    elif isinstance(value, list):
        raw = value
    else:
        raw = list(value)
    return tuple(
        {field: row.get(field) for field in fields} for row in raw if isinstance(row, dict)
    )


def _single_display(
    row: dict[str, object],
    *,
    match_labels: dict[str, str],
) -> dict[str, object]:
    reasons = [str(item) for item in _list(row.get("reason_codes"))]
    event_id = str(row.get("canonical_event_id", ""))
    return {
        "match": match_labels.get(event_id, event_id),
        "selection": str(row.get("outcome_key", "")),
        "market": str(row.get("market_family", "")),
        "provider": row.get("provider_id") or "—",
        "offered odd": row.get("offered_decimal_odds") or "—",
        "fair odd": row.get("fair_decimal_odds") or "—",
        "model probability": _number(row.get("model_probability")) * 100,
        "edge": _number(row.get("edge")) * 100,
        "EV": _number(row.get("expected_value")) * 100,
        "status": (
            "ready-for-manual-placement"
            if row.get("accepted") is True
            else "held"
            if reasons and row.get("offered_decimal_odds") is not None
            else "rejected"
            if row.get("offered_decimal_odds") is None
            else "analytical-candidate"
        ),
        "hold reason": ", ".join(reasons) or "—",
        "updated time": str(row.get("decision_as_of_utc", "")),
    }


def _signature(rows: tuple[dict[str, object], ...]) -> str:
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _safe(exc: BaseException) -> str:
    return str(exc).replace("\r", " ").replace("\n", " ").strip()[:300] or type(exc).__name__


def _short(value: str) -> str:
    return value if len(value) <= 18 else f"{value[:8]}…{value[-8:]}"


def _list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


def _percent(value: object) -> str:
    return f"{_number(value):.2%}"
