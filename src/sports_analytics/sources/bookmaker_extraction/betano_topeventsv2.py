"""Verified Betano PT football pre-match extraction from ``data.topEventsV2``.

Recognition contract
--------------------
Recognizes public Betano JSON objects that contain::

    data.topEventsV2.{eventIdList, events, leagues, markets, selections, sports, ...}

without depending on exact live event IDs, team names, timestamps, prices, or
dictionary sizes. Only football (``sportId == "FOOT"``) pre-match events with
exactly two participants are admitted.

Supported markets (provider type → adapter contract code):

- ``MRES`` / typeId 1 → football 1X2 (``MRES``)
- ``HCTG`` / typeId 13 → football total goals with exact handicap line (``TOTG``)
- ``BTSC`` / typeId 15 → both teams to score (``BTTS``)

Selection mapping requires compatible provider ``typeId`` (labels are an
additional consistency check only). Sparse ``isSuspended`` absence means
not-suspended **only** for this reviewed profile (documented + tested).

Rules/overtime/settlement semantics are **not** invented: adapter-contract
``rulesScope`` / ``overtimeScope`` are omitted so quotes remain
``comparable=false`` until rules evidence is reviewed.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

from sports_analytics.core.exceptions import ParserError
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.snapshots.paths import resolve_raw_path
from sports_analytics.sources.betano.catalog import PROVIDER_ID
from sports_analytics.sources.bookmaker_extraction.adapter_contract import load_json_payload
from sports_analytics.sources.bookmaker_extraction.contracts import (
    ADAPTER_CONTRACT_SCHEMA,
    ExtractionResult,
)
from sports_analytics.sources.browser.contracts import BrowserAcquisitionResult
from sports_analytics.sources.raw_capture import BookmakerRawCapture
from sports_analytics.sports.contracts import require_utc

BETANO_TOPEVENTSV2_PROFILE_ID: Final[str] = "betano-pt-football-topeventsv2-v1"
BETANO_FOOTBALL_SPORT_ID: Final[str] = "FOOT"

_SUPPORTED_MARKET_TYPES: Final[dict[str, tuple[int, str]]] = {
    # provider type -> (expected typeId, adapter marketTypeCode)
    "MRES": (1, "MRES"),
    "HCTG": (13, "TOTG"),
    "BTSC": (15, "BTTS"),
}

_MRES_SELECTION_TYPE_IDS: Final[dict[int, tuple[str, frozenset[str]]]] = {
    1: ("1", frozenset({"1", "home"})),
    2: ("X", frozenset({"x", "draw"})),
    3: ("2", frozenset({"2", "away"})),
}
_HCTG_SELECTION_TYPE_IDS: Final[dict[int, tuple[str, frozenset[str]]]] = {
    39: ("over", frozenset({"over", "mais"})),
    40: ("under", frozenset({"under", "menos"})),
}
_BTSC_SELECTION_TYPE_IDS: Final[dict[int, tuple[str, frozenset[str]]]] = {
    43: ("yes", frozenset({"yes", "sim"})),
    44: ("no", frozenset({"no", "não", "nao"})),
}


class BetanoFootballTopEventsV2Profile:
    """Strict verified football pre-match profile for Betano ``topEventsV2``."""

    profile_id = BETANO_TOPEVENTSV2_PROFILE_ID
    verified = True
    provider_id = PROVIDER_ID
    sport = "football"
    schema_version = "betano-topeventsv2-profile-v1"
    supported_capture_surfaces = ("sport-landing-popular-events",)
    completeness_capability = "landing-inventory-only"
    market_extraction_capability = "reviewed-canonical-subset"
    raw_directory: Path | None = None

    def extract(
        self,
        *,
        browser_result: BrowserAcquisitionResult,
        captures: tuple[BookmakerRawCapture, ...],
    ) -> ExtractionResult:
        payloads: list[dict[str, object]] = []
        warnings: list[str] = []
        drift_codes: list[str] = []
        evidence_times: list[datetime] = []
        recognized_response_count = 0

        for response in browser_result.responses:
            try:
                raw = load_json_payload(response.body_text)
            except ParserError as exc:
                warnings.append(str(exc))
                drift_codes.append("json-parse-failed")
                continue
            if not looks_like_topeventsv2(raw):
                continue
            recognized_response_count += 1
            try:
                translated = translate_topeventsv2(
                    raw,
                    observed_at_utc=require_utc(
                        response.observed_at_utc,
                        field_name="response.observed_at_utc",
                    ),
                )
            except ParserError as exc:
                warnings.append(str(exc))
                drift_codes.append("topeventsv2-rejected")
                continue
            payloads.append(translated["payload"])
            evidence_times.append(response.observed_at_utc)
            drift_codes.extend(translated["drift_codes"])
            warnings.extend(translated["warnings"])

        if not payloads and captures:
            for capture in captures:
                if capture.capture_kind != "provider-json":
                    continue
                try:
                    text = _read_capture_text(capture, raw_directory=self.raw_directory)
                    raw = load_json_payload(text)
                except ParserError as exc:
                    warnings.append(str(exc))
                    drift_codes.append("json-parse-failed")
                    continue
                if not looks_like_topeventsv2(raw):
                    continue
                observed = require_utc(capture.retrieved_at, field_name="retrieved_at")
                try:
                    translated = translate_topeventsv2(raw, observed_at_utc=observed)
                except ParserError as exc:
                    warnings.append(str(exc))
                    drift_codes.append("topeventsv2-rejected")
                    continue
                payloads.append(translated["payload"])
                evidence_times.append(observed)
                drift_codes.extend(translated["drift_codes"])
                warnings.extend(translated["warnings"])

        if not payloads:
            if browser_result.responses or captures:
                drift_codes.append("no-topeventsv2-payload")
            return ExtractionResult(
                profile_id=self.profile_id,
                verified=self.verified,
                adapter_contract_payloads=(),
                drift_codes=tuple(sorted(set(drift_codes))),
                warnings=tuple(warnings),
                recognized_response_count=recognized_response_count,
            )

        # Deterministic evidence timestamp policy: latest contributing response.
        _ = max(evidence_times)
        return ExtractionResult(
            profile_id=self.profile_id,
            verified=self.verified,
            adapter_contract_payloads=tuple(payloads),
            drift_codes=tuple(sorted(set(drift_codes))),
            warnings=tuple(warnings),
            recognized_response_count=recognized_response_count,
        )


def looks_like_topeventsv2(raw: dict[str, Any]) -> bool:
    """Return whether payload contains the reviewed ``data.topEventsV2`` shape."""
    data = raw.get("data")
    if not isinstance(data, dict):
        return False
    top = data.get("topEventsV2")
    if not isinstance(top, dict):
        return False
    required = ("eventIdList", "events", "markets", "selections")
    return all(key in top for key in required)


def translate_topeventsv2(
    raw: dict[str, Any],
    *,
    observed_at_utc: datetime,
) -> dict[str, Any]:
    """Translate one ``topEventsV2`` object into an adapter-contract payload."""
    observed = require_utc(observed_at_utc, field_name="observed_at_utc")
    top = raw["data"]["topEventsV2"]
    assert isinstance(top, dict)
    events_dict = top.get("events")
    markets_dict = top.get("markets")
    selections_dict = top.get("selections")
    leagues_dict = top.get("leagues")
    event_id_list = top.get("eventIdList")
    if not isinstance(events_dict, dict) or not isinstance(markets_dict, dict):
        msg = "topeventsv2 events/markets must be dictionaries"
        raise ParserError(msg)
    if not isinstance(selections_dict, dict):
        msg = "topeventsv2 selections must be a dictionary"
        raise ParserError(msg)
    if not isinstance(event_id_list, list):
        msg = "topeventsv2 eventIdList must be a list"
        raise ParserError(msg)

    seen_event_ids: set[str] = set()
    events_out: list[dict[str, object]] = []
    unsupported: list[dict[str, object]] = []
    warnings: list[str] = []
    drift_codes: list[str] = []

    for event_id_raw in event_id_list:
        event_id = str(event_id_raw)
        if event_id in seen_event_ids:
            msg = f"duplicate event id in eventIdList: {event_id}"
            raise ParserError(msg)
        seen_event_ids.add(event_id)
        event = events_dict.get(event_id)
        if event is None:
            # Malformed reference: auditable drift, fail closed for that event.
            drift_codes.append("malformed-event-reference")
            warnings.append(f"eventIdList references missing event {event_id}")
            continue
        if not isinstance(event, dict):
            drift_codes.append("malformed-event")
            continue
        if bool(event.get("isLive")):
            drift_codes.append("live-event-excluded")
            continue
        sport_id = str(event.get("sportId") or "")
        if sport_id != BETANO_FOOTBALL_SPORT_ID:
            drift_codes.append("non-football-excluded")
            continue
        try:
            start = _parse_epoch_ms(event.get("startTime"))
        except ParserError as exc:
            warnings.append(str(exc))
            drift_codes.append("invalid-start-time")
            continue
        if start <= observed:
            drift_codes.append("non-prematch-excluded")
            continue
        participants_raw = event.get("participants")
        if not isinstance(participants_raw, list) or len(participants_raw) != 2:
            drift_codes.append("participant-count-rejected")
            continue
        participants_out: list[dict[str, object]] = []
        seen_participant_ids: set[str] = set()
        roles = ("HOME", "AWAY")
        valid_participants = True
        for index, participant in enumerate(participants_raw):
            if not isinstance(participant, dict):
                valid_participants = False
                break
            team_id = participant.get("teamId")
            name = participant.get("name")
            if team_id is None or name is None:
                valid_participants = False
                break
            participant_id = str(team_id)
            if participant_id in seen_participant_ids:
                valid_participants = False
                break
            seen_participant_ids.add(participant_id)
            participants_out.append(
                {
                    "participantId": participant_id,
                    "name": str(name),
                    "role": roles[index],
                    "normalizedName": str(name).casefold(),
                }
            )
        if not valid_participants:
            drift_codes.append("participant-identity-rejected")
            continue

        market_id_list = event.get("marketIdList")
        if not isinstance(market_id_list, list):
            drift_codes.append("malformed-market-list")
            continue
        markets_out: list[dict[str, object]] = []
        native_markets_out: list[dict[str, object]] = []
        seen_market_ids: set[str] = set()
        for market_id_raw in market_id_list:
            market_id = str(market_id_raw)
            if market_id in seen_market_ids:
                msg = f"duplicate market id on event {event_id}: {market_id}"
                raise ParserError(msg)
            seen_market_ids.add(market_id)
            market = markets_dict.get(market_id)
            if market is None:
                drift_codes.append("malformed-market-reference")
                warnings.append(f"marketIdList references missing market {market_id}")
                continue
            if not isinstance(market, dict):
                drift_codes.append("malformed-market")
                continue
            mapped = _translate_market(
                market_id=market_id,
                market=market,
                selections_dict=selections_dict,
            )
            if mapped["kind"] == "unsupported":
                record = mapped["record"]
                assert isinstance(record, dict)
                native_markets_out.append(record)
                unsupported.append(
                    {
                        "marketId": record["marketId"],
                        "name": record["name"],
                        "providerType": record["providerType"],
                        "providerTypeId": record["providerTypeId"],
                    }
                )
                drift_codes.append("unknown-market")
                continue
            if mapped["kind"] == "rejected":
                drift_codes.append(str(mapped["drift"]))
                warnings.append(str(mapped["warning"]))
                continue
            record = mapped["record"]
            assert isinstance(record, dict)
            markets_out.append(record)
            native_markets_out.append(record)

        if not markets_out:
            drift_codes.append("event-without-supported-markets")
            continue

        league_id = str(event.get("leagueId") or "unknown-league")
        competition_name = None
        if isinstance(leagues_dict, dict):
            league = leagues_dict.get(league_id)
            if isinstance(league, dict) and league.get("name") is not None:
                competition_name = str(league["name"])

        events_out.append(
            {
                "eventId": event_id,
                "competitionId": league_id,
                "competitionName": competition_name,
                "sportCode": "FOOT",
                "state": "NOT_STARTED",
                "startTimeUtc": format_utc_timestamp(start),
                "sourcePageRouteId": "football-prematch",
                "participants": participants_out,
                "markets": markets_out,
                "nativeMarkets": native_markets_out,
            }
        )

    return {
        "payload": {
            "schema": ADAPTER_CONTRACT_SCHEMA,
            "events": events_out,
            "unsupportedMarkets": unsupported,
        },
        "warnings": warnings,
        "drift_codes": drift_codes,
        "evidence_observed_at_utc": observed,
    }


def _translate_market(
    *,
    market_id: str,
    market: dict[str, Any],
    selections_dict: dict[str, Any],
) -> dict[str, Any]:
    provider_type = str(market.get("type") or "")
    type_id_raw = market.get("typeId")
    try:
        type_id = int(type_id_raw) if type_id_raw is not None else None
    except (TypeError, ValueError):
        type_id = None
    mapping = _SUPPORTED_MARKET_TYPES.get(provider_type)
    if mapping is None:
        generic_selections: list[dict[str, object]] = []
        selection_id_list = market.get("selectionIdList")
        if not isinstance(selection_id_list, list) or not selection_id_list:
            return {
                "kind": "rejected",
                "drift": "malformed-selection-list",
                "warning": f"market {market_id} missing selectionIdList",
            }
        for selection_id_raw in selection_id_list:
            selection_id = str(selection_id_raw)
            selection = selections_dict.get(selection_id)
            if not isinstance(selection, dict):
                return {
                    "kind": "rejected",
                    "drift": "malformed-selection-reference",
                    "warning": f"selectionIdList references missing selection {selection_id}",
                }
            translated = _translate_native_selection(
                selection_id=selection_id,
                selection=selection,
            )
            if translated is None:
                return {
                    "kind": "rejected",
                    "drift": "malformed-selection",
                    "warning": f"selection {selection_id} has invalid native price evidence",
                }
            generic_selections.append(translated)
        return {
            "kind": "unsupported",
            "record": {
                "marketId": market_id,
                "marketTypeCode": provider_type,
                "name": str(market.get("name") or provider_type or market_id),
                "status": (
                    "SUSPENDED" if _sparse_is_suspended(market.get("isSuspended")) else "OPEN"
                ),
                "period": str(market.get("period") or "FT"),
                "selections": generic_selections,
                "providerType": provider_type,
                "providerTypeId": type_id,
            },
        }
    expected_type_id, adapter_code = mapping
    if type_id != expected_type_id:
        return {
            "kind": "rejected",
            "drift": "market-typeid-mismatch",
            "warning": (
                f"market {market_id} type {provider_type!r} typeId {type_id!r} "
                f"does not match reviewed typeId {expected_type_id}"
            ),
        }

    suspended = _sparse_is_suspended(market.get("isSuspended"))
    status = "SUSPENDED" if suspended else "OPEN"
    selection_id_list = market.get("selectionIdList")
    if not isinstance(selection_id_list, list) or not selection_id_list:
        return {
            "kind": "rejected",
            "drift": "malformed-selection-list",
            "warning": f"market {market_id} missing selectionIdList",
        }

    selections_out: list[dict[str, object]] = []
    seen_selection_ids: set[str] = set()
    market_line: Decimal | None = None
    for selection_id_raw in selection_id_list:
        selection_id = str(selection_id_raw)
        if selection_id in seen_selection_ids:
            return {
                "kind": "rejected",
                "drift": "duplicate-selection-id",
                "warning": f"duplicate selection id {selection_id} on market {market_id}",
            }
        seen_selection_ids.add(selection_id)
        selection = selections_dict.get(selection_id)
        if selection is None:
            return {
                "kind": "rejected",
                "drift": "malformed-selection-reference",
                "warning": f"selectionIdList references missing selection {selection_id}",
            }
        if not isinstance(selection, dict):
            return {
                "kind": "rejected",
                "drift": "malformed-selection",
                "warning": f"selection {selection_id} is not an object",
            }
        mapped_selection = _translate_selection(
            provider_type=provider_type,
            selection_id=selection_id,
            selection=selection,
        )
        if mapped_selection is None:
            return {
                "kind": "rejected",
                "drift": "selection-mapping-rejected",
                "warning": f"selection {selection_id} failed typeId/label mapping",
            }
        if mapped_selection.get("line") is not None:
            line_value = Decimal(str(mapped_selection["line"]))
            if market_line is None:
                market_line = line_value
            elif market_line != line_value:
                return {
                    "kind": "rejected",
                    "drift": "contradictory-line",
                    "warning": f"market {market_id} has contradictory selection lines",
                }
        selections_out.append(mapped_selection)

    if adapter_code == "TOTG" and market_line is None:
        return {
            "kind": "rejected",
            "drift": "missing-total-line",
            "warning": f"HCTG market {market_id} missing handicap line",
        }

    record: dict[str, object] = {
        "marketId": market_id,
        "marketTypeCode": adapter_code,
        "name": str(market.get("name") or provider_type),
        "status": status,
        "period": "FT",
        "selections": selections_out,
        # Persist exact provider codes without inventing settlement semantics.
        "providerType": provider_type,
        "providerTypeId": type_id,
    }
    if market_line is not None:
        record["line"] = format(market_line, "f")
    return {"kind": "supported", "record": record}


def _translate_selection(
    *,
    provider_type: str,
    selection_id: str,
    selection: dict[str, Any],
) -> dict[str, object] | None:
    try:
        type_id = int(selection["typeId"])
    except (KeyError, TypeError, ValueError):
        return None
    price_raw = selection.get("price")
    try:
        price = Decimal(str(price_raw).replace(",", "."))
    except (InvalidOperation, AttributeError, TypeError):
        return None
    if not price.is_finite() or price <= Decimal("1"):
        return None

    table: dict[int, tuple[str, frozenset[str]]]
    if provider_type == "MRES":
        table = _MRES_SELECTION_TYPE_IDS
    elif provider_type == "HCTG":
        table = _HCTG_SELECTION_TYPE_IDS
    elif provider_type == "BTSC":
        table = _BTSC_SELECTION_TYPE_IDS
    else:
        return None
    mapped = table.get(type_id)
    if mapped is None:
        return None
    canonical_name, allowed_labels = mapped
    label = str(selection.get("name") or "").strip().casefold()
    if label and label not in allowed_labels:
        # Labels are an additional consistency check only.
        return None

    suspended = _sparse_is_suspended(selection.get("isSuspended"))
    out: dict[str, object] = {
        "selectionId": selection_id,
        "name": canonical_name,
        "price": format(price, "f"),
        "status": "SUSPENDED" if suspended else "ACTIVE",
        "providerTypeId": type_id,
    }
    if provider_type == "HCTG":
        handicap = selection.get("handicap")
        if handicap is None:
            return None
        try:
            line = Decimal(str(handicap).replace(",", "."))
        except (InvalidOperation, TypeError):
            return None
        if not line.is_finite():
            return None
        out["line"] = format(line, "f")
    return out


def _translate_native_selection(
    *,
    selection_id: str,
    selection: dict[str, Any],
) -> dict[str, object] | None:
    """Preserve an observed unknown selection without assigning semantics."""
    price_raw = selection.get("price")
    try:
        price = Decimal(str(price_raw).replace(",", "."))
    except (InvalidOperation, AttributeError, TypeError):
        return None
    if not price.is_finite() or price <= Decimal("1"):
        return None
    out: dict[str, object] = {
        "selectionId": selection_id,
        "name": str(selection.get("name") or selection_id),
        "price": format(price, "f"),
        "status": ("SUSPENDED" if _sparse_is_suspended(selection.get("isSuspended")) else "ACTIVE"),
    }
    if selection.get("typeId") is not None:
        out["providerTypeId"] = str(selection["typeId"])
    handicap = selection.get("handicap")
    if handicap is not None:
        try:
            line = Decimal(str(handicap).replace(",", "."))
        except (InvalidOperation, TypeError):
            return None
        if not line.is_finite():
            return None
        out["line"] = format(line, "f")
    return out


def _sparse_is_suspended(value: object) -> bool:
    """Reviewed Betano sparse-field rule: absent ``isSuspended`` means false."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            return False
        return value != 0
    text = str(value).strip().casefold()
    return text in {"true", "1", "yes"}


def _parse_epoch_ms(value: object) -> datetime:
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            msg = f"invalid startTime epoch milliseconds: {value!r}"
            raise ParserError(msg)
        millis = int(value)
    except (TypeError, ValueError) as exc:
        msg = f"invalid startTime epoch milliseconds: {value!r}"
        raise ParserError(msg) from exc
    if millis < 1_000_000_000_000:
        msg = f"startTime must be epoch milliseconds, got {millis}"
        raise ParserError(msg)
    return datetime.fromtimestamp(millis / 1000.0, tz=UTC)


def _read_capture_text(
    capture: BookmakerRawCapture,
    *,
    raw_directory: Path | None,
) -> str:
    if raw_directory is None:
        msg = "topeventsv2 profile requires raw_directory when reading captures"
        raise ParserError(msg)
    return resolve_raw_path(raw_directory, capture.relative_path).read_text(encoding="utf-8")


BETANO_FOOTBALL_TOPEVENTSV2_PROFILE = BetanoFootballTopEventsV2Profile()
