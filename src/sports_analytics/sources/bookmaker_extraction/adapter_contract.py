"""Internal adapter-contract parser shared by verified/example extraction profiles."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sports_analytics.core.exceptions import NormalizationError, ParserError
from sports_analytics.markets.contracts import MarketStatus, SelectionStatus
from sports_analytics.sources.bookmaker_contracts import (
    ParserDriftSeverity,
    ProviderAcquisitionBundle,
    ProviderEventObservation,
    ProviderEventState,
    ProviderMarketObservation,
    ProviderParserWarning,
    ProviderParticipantObservation,
    ProviderSelectionObservation,
    ProviderSelectionPriceState,
)
from sports_analytics.sources.bookmaker_extraction.contracts import ADAPTER_CONTRACT_SCHEMA
from sports_analytics.sports.contracts import require_utc

_EVENT_STATE: dict[str, ProviderEventState] = {
    "NOT_STARTED": ProviderEventState.PRE_MATCH,
    "PREMATCH": ProviderEventState.PRE_MATCH,
}
_MARKET_STATUS: dict[str, MarketStatus] = {
    "OPEN": MarketStatus.OPEN,
    "SUSPENDED": MarketStatus.SUSPENDED,
    "CLOSED": MarketStatus.CLOSED,
}
_SELECTION_STATUS: dict[str, SelectionStatus] = {
    "ACTIVE": SelectionStatus.ACTIVE,
    "SUSPENDED": SelectionStatus.SUSPENDED,
}

_PARTICIPANT_ROLES: dict[str, str] = {
    "HOME": "home",
    "AWAY": "away",
    "PLAYER1": "player-1",
    "PLAYER2": "player-2",
}

_SPORT_CODES: dict[str, str] = {
    "FOOT": "football",
    "BASK": "basketball",
    "TENN": "tennis",
}

_PERIOD_CODES: dict[str, str] = {
    "FT": "full-match",
    "REG": "regular-time",
    "OT": "including-overtime",
}

_MARKET_TYPE_CODES: dict[str, str] = {
    "MRES": "football-match-result-1x2",
    "TOTG": "football-total-goals",
    "BTTS": "football-btts",
    "MWIN": "basketball-match-winner-with-ot",
    "TOTP": "basketball-total-points-with-ot",
    "SPRD": "basketball-spread-with-ot",
    "TMWN": "tennis-match-winner",
}


def parse_adapter_contract_payloads(
    payloads: list[dict[str, Any]],
    *,
    provider_id: str,
    adapter_version: str,
    acquisition_cycle_id: str,
    observed_at_utc: datetime,
    sport: str,
    extra_warnings: tuple[ProviderParserWarning, ...] = (),
    provenance: tuple[str, ...] = (),
    parser_version: str,
) -> ProviderAcquisitionBundle:
    """Parse strict internal adapter-contract envelopes (not provider-native JSON)."""
    events: list[ProviderEventObservation] = []
    warnings = list(extra_warnings)
    drift_codes: set[str] = set()
    contract_recognized = False
    for payload in payloads:
        schema = payload.get("schema")
        if schema != ADAPTER_CONTRACT_SCHEMA:
            drift_codes.add("unknown-schema")
            warnings.append(
                ProviderParserWarning(
                    code="schema-drift",
                    message=f"unexpected schema {schema!r}",
                    severity=ParserDriftSeverity.ERROR,
                )
            )
            continue
        contract_recognized = True
        for raw_event in payload.get("events", []):
            if not isinstance(raw_event, dict):
                drift_codes.add("malformed-event")
                continue
            try:
                events.append(_parse_event(raw_event))
            except (NormalizationError, ParserError) as exc:
                warnings.append(
                    ProviderParserWarning(
                        code="event-rejected",
                        message=str(exc),
                        severity=ParserDriftSeverity.WARNING,
                    )
                )
        for unsupported in payload.get("unsupportedMarkets", []):
            drift_codes.add("unknown-market")
            label = unsupported.get("name") if isinstance(unsupported, dict) else str(unsupported)
            warnings.append(
                ProviderParserWarning(
                    code="unknown-market-retained",
                    message=f"unknown market retained for audit: {label}",
                    severity=ParserDriftSeverity.WARNING,
                )
            )
    if not contract_recognized and payloads:
        drift_codes.add("no-adapter-contract")
    return ProviderAcquisitionBundle(
        provider_id=provider_id,
        adapter_version=adapter_version,
        acquisition_cycle_id=acquisition_cycle_id,
        observed_at_utc=observed_at_utc,
        sport=sport,
        events=tuple(events),
        warnings=tuple(warnings),
        drift_codes=tuple(sorted(drift_codes)),
        provenance=tuple(sorted(set(provenance) | {f"parser:{parser_version}"})),
    )


def load_json_payload(body_text: str) -> dict[str, Any]:
    """Load one JSON object payload from captured text."""
    try:
        loaded = json.loads(body_text)
    except json.JSONDecodeError as exc:
        msg = f"invalid JSON payload: {exc}"
        raise ParserError(msg) from exc
    if not isinstance(loaded, dict):
        msg = "payload must be a JSON object"
        raise ParserError(msg)
    return loaded


def _parse_event(raw: dict[str, Any]) -> ProviderEventObservation:
    sport_code = str(raw.get("sportCode") or "")
    sport = _map_sport_code(sport_code)
    state_raw = str(raw.get("state") or "").upper()
    if not state_raw:
        msg = "missing event state"
        raise ParserError(msg)
    state = _EVENT_STATE.get(state_raw)
    if state is None:
        msg = f"unsupported event state {state_raw!r}"
        raise ParserError(msg)
    if state is not ProviderEventState.PRE_MATCH:
        msg = "live events are not supported in PR #11"
        raise ParserError(msg)
    participants = tuple(
        ProviderParticipantObservation(
            source_participant_id=str(item["participantId"]),
            display_name=str(item["name"]),
            role=_map_participant_role(str(item.get("role", "")).upper()),
            normalized_name=(
                str(item["normalizedName"]).lower()
                if item.get("normalizedName") is not None
                else None
            ),
        )
        for item in raw.get("participants", [])
        if isinstance(item, dict)
    )
    native_market_payload = raw.get("nativeMarkets", raw.get("markets", []))
    native_markets = tuple(
        _parse_market(item) for item in native_market_payload if isinstance(item, dict)
    )
    markets = tuple(
        market for market in native_markets if market.canonical_market_definition_id is not None
    )
    start = raw.get("startTimeUtc")
    if not isinstance(start, str):
        msg = "malformed startTimeUtc"
        raise ParserError(msg)
    try:
        scheduled = require_utc(
            datetime.fromisoformat(start.replace("Z", "+00:00")),
            field_name="startTimeUtc",
        )
    except (TypeError, ValueError, NormalizationError) as exc:
        msg = "malformed startTimeUtc"
        raise ParserError(msg) from exc
    return ProviderEventObservation(
        source_event_id=str(raw["eventId"]),
        source_competition_id=str(raw["competitionId"]),
        sport=sport,
        scheduled_start_utc=scheduled,
        event_state=state,
        participants=participants,
        markets=markets,
        source_page_route_id=str(raw.get("sourcePageRouteId") or f"{sport}-prematch"),
        competition_display_name=(
            str(raw["competitionName"]) if raw.get("competitionName") is not None else None
        ),
        native_markets=native_markets,
    )


def _parse_market(raw: dict[str, Any]) -> ProviderMarketObservation:
    type_code = str(raw.get("marketTypeCode") or "")
    canonical = _map_market_type(type_code)
    status_raw = str(raw.get("status") or "").upper()
    if not status_raw:
        msg = "missing market status"
        raise ParserError(msg)
    market_status = _MARKET_STATUS.get(status_raw)
    if market_status is None:
        msg = f"unsupported market status {status_raw!r}"
        raise ParserError(msg)
    period_code = raw.get("period")
    period = None
    if period_code is not None:
        period = _PERIOD_CODES.get(str(period_code).upper())
        if period is None:
            if canonical is None:
                period = str(period_code)
            else:
                msg = f"unsupported market period {period_code!r}"
                raise ParserError(msg)
    overtime_scope = raw.get("overtimeScope")
    if overtime_scope is not None and str(overtime_scope) not in {"none", "including-overtime"}:
        msg = f"unsupported overtime scope {overtime_scope!r}"
        raise ParserError(msg)
    rules_scope = raw.get("rulesScope")
    if rules_scope is not None and str(rules_scope) not in {"regular-time", "including-overtime"}:
        msg = f"unsupported rules scope {rules_scope!r}"
        raise ParserError(msg)
    selections: list[ProviderSelectionObservation] = []
    seen: set[str] = set()
    for item in raw.get("selections", []):
        if not isinstance(item, dict):
            continue
        sel_id = str(item.get("selectionId", ""))
        if not sel_id or sel_id in seen:
            msg = "duplicate source selection identities"
            raise ParserError(msg)
        seen.add(sel_id)
        price = item.get("price")
        if price is None:
            odds = None
            price_state = ProviderSelectionPriceState.UNPRICED
        else:
            try:
                odds = Decimal(str(price).replace(",", "."))
            except InvalidOperation as exc:
                msg = "invalid decimal odds"
                raise ParserError(msg) from exc
            price_state = ProviderSelectionPriceState.PRICED
        status_value = str(item.get("status") or "").upper()
        if not status_value:
            msg = "missing selection status"
            raise ParserError(msg)
        sel_status = _SELECTION_STATUS.get(status_value)
        if sel_status is None:
            msg = f"unsupported selection status {status_value!r}"
            raise ParserError(msg)
        line = None
        if item.get("line") is not None:
            line = Decimal(str(item["line"]).replace(",", "."))
        selections.append(
            ProviderSelectionObservation(
                source_selection_id=sel_id,
                display_label=str(item.get("name", sel_id)),
                decimal_odds=odds,
                selection_status=sel_status,
                price_state=price_state,
                line=line,
                provider_selection_type=(
                    str(item["providerTypeId"]) if item.get("providerTypeId") is not None else None
                ),
                provider_order=len(selections),
                source_capture_id=(
                    str(item["sourceCaptureId"])
                    if item.get("sourceCaptureId") is not None
                    else None
                ),
            )
        )
    market_line = None
    if raw.get("line") is not None:
        market_line = Decimal(str(raw["line"]).replace(",", "."))
    return ProviderMarketObservation(
        source_market_id=str(raw["marketId"]),
        display_label=str(raw.get("name", type_code)),
        market_status=market_status,
        selections=tuple(selections),
        period=period,
        line=market_line,
        overtime_scope=(str(overtime_scope) if overtime_scope is not None else None),
        rules_scope=(str(rules_scope) if rules_scope is not None else None),
        canonical_market_definition_id=canonical,
        provider_market_type=(
            str(raw["providerType"]) if raw.get("providerType") is not None else type_code
        ),
        provider_order=0,
        source_capture_id=(
            str(raw["sourceCaptureId"]) if raw.get("sourceCaptureId") is not None else None
        ),
    )


def _map_sport_code(code: str) -> str:
    if not code:
        msg = "missing sport code"
        raise ParserError(msg)
    mapped = _SPORT_CODES.get(code.upper())
    if mapped is None:
        msg = f"unsupported sport code {code!r}"
        raise ParserError(msg)
    return mapped


def _map_participant_role(code: str) -> str:
    if not code:
        msg = "missing participant role"
        raise ParserError(msg)
    mapped = _PARTICIPANT_ROLES.get(code)
    if mapped is None:
        msg = f"unsupported participant role {code!r}"
        raise ParserError(msg)
    return mapped


def _map_market_type(code: str) -> str | None:
    if not code:
        return None
    return _MARKET_TYPE_CODES.get(code.upper())
