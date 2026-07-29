"""Example/synthetic Betano extraction profile for tests only (not provider-native)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sports_analytics.core.exceptions import ParserError
from sports_analytics.snapshots.paths import resolve_raw_path
from sports_analytics.sources.betano.catalog import PROVIDER_ID
from sports_analytics.sources.bookmaker_extraction.adapter_contract import load_json_payload
from sports_analytics.sources.bookmaker_extraction.contracts import (
    ADAPTER_CONTRACT_SCHEMA,
    ExtractionResult,
)
from sports_analytics.sources.browser.contracts import BrowserAcquisitionResult
from sports_analytics.sources.raw_capture import BookmakerRawCapture

EXAMPLE_BETANO_PROFILE_ID = "example-betano-synthetic-v1"
SYNTHETIC_FIXTURE_SCHEMA = "betano-fixture-bundle-v1"


class ExampleBetanoSyntheticExtractionProfile:
    """Translate test-only ``betano-fixture-bundle-v1`` captures to adapter contract."""

    profile_id = EXAMPLE_BETANO_PROFILE_ID
    verified = False
    provider_id = PROVIDER_ID
    sport = "football"
    raw_directory: Path | None = None

    def extract(
        self,
        *,
        browser_result: BrowserAcquisitionResult,
        captures: tuple[BookmakerRawCapture, ...],
    ) -> ExtractionResult:
        del browser_result
        payloads: list[dict[str, object]] = []
        warnings: list[str] = []
        drift_codes: list[str] = []
        for capture in captures:
            if capture.capture_kind != "provider-json":
                continue
            try:
                raw = load_json_payload(
                    _read_capture_text(capture, raw_directory=self.raw_directory)
                )
            except ParserError as exc:
                warnings.append(str(exc))
                drift_codes.append("json-parse-failed")
                continue
            schema = raw.get("schema")
            if schema != SYNTHETIC_FIXTURE_SCHEMA:
                drift_codes.append("unknown-schema")
                warnings.append(f"unexpected schema {schema!r}")
                continue
            payloads.append(_translate_fixture_bundle(raw))
        if not payloads and captures:
            drift_codes.append("no-example-payload")
        return ExtractionResult(
            profile_id=self.profile_id,
            verified=self.verified,
            adapter_contract_payloads=tuple(payloads),
            drift_codes=tuple(sorted(set(drift_codes))),
            warnings=tuple(warnings),
        )


def _read_capture_text(
    capture: BookmakerRawCapture,
    *,
    raw_directory: Path | None,
) -> str:
    if raw_directory is not None:
        return resolve_raw_path(raw_directory, capture.relative_path).read_text(encoding="utf-8")
    msg = "example profile requires raw_directory to resolve capture bytes in tests"
    raise ParserError(msg)


def _translate_fixture_bundle(raw: dict[str, Any]) -> dict[str, object]:
    """Map synthetic fixture bundle fields into the internal adapter contract."""
    events: list[dict[str, object]] = []
    for item in raw.get("events", []):
        if not isinstance(item, dict):
            continue
        events.append(
            {
                "eventId": item.get("event_id")
                or item.get("eventId")
                or item.get("source_event_id"),
                "competitionId": item.get("competition_id")
                or item.get("competitionId")
                or item.get("source_competition_id"),
                "competitionName": item.get("competition_name")
                or item.get("competitionName")
                or item.get("competition_display_name"),
                "sportCode": _sport_code(item.get("sport") or item.get("sportCode")),
                "state": "NOT_STARTED",
                "startTimeUtc": item.get("start_time_utc")
                or item.get("startTimeUtc")
                or item.get("scheduled_start_utc"),
                "sourcePageRouteId": item.get("source_page_route_id")
                or item.get("sourcePageRouteId"),
                "participants": [
                    {
                        "participantId": p.get("participant_id")
                        or p.get("participantId")
                        or p.get("source_participant_id"),
                        "name": p.get("name") or p.get("display_name"),
                        "role": (p.get("role") or "HOME").upper(),
                        "normalizedName": p.get("normalized_name") or p.get("normalizedName"),
                    }
                    for p in item.get("participants", [])
                    if isinstance(p, dict)
                ],
                "markets": [
                    _translate_market(market)
                    for market in item.get("markets", [])
                    if isinstance(market, dict)
                ],
            }
        )
    unsupported = raw.get("unknown_markets") or raw.get("unsupportedMarkets") or []
    return {
        "schema": ADAPTER_CONTRACT_SCHEMA,
        "events": events,
        "unsupportedMarkets": list(unsupported),
    }


_SYNTHETIC_MARKET_CODE_BY_CANONICAL: dict[str, str] = {
    "football-match-result-1x2": "MRES",
    "football-total-goals": "TOTG",
    "football-btts": "BTTS",
    "basketball-match-winner-with-ot": "MWIN",
    "basketball-total-points-with-ot": "TOTP",
    "basketball-spread-with-ot": "SPRD",
    "tennis-match-winner": "TMWN",
}


def _period_code(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    mapping = {
        "full-match": "FT",
        "regular-time": "REG",
        "including-overtime": "OT",
        "FT": "FT",
        "REG": "REG",
        "OT": "OT",
    }
    return mapping.get(text) or mapping.get(text.upper())


def _translate_market(raw: dict[str, Any]) -> dict[str, object]:
    canonical = raw.get("canonical_market_definition_id")
    market_type = (
        raw.get("market_type_code")
        or raw.get("marketTypeCode")
        or (_SYNTHETIC_MARKET_CODE_BY_CANONICAL.get(str(canonical)) if canonical else None)
        or "MRES"
    )
    return {
        "marketId": raw.get("market_id") or raw.get("marketId") or raw.get("source_market_id"),
        "marketTypeCode": market_type,
        "name": raw.get("name") or raw.get("display_label"),
        "status": (raw.get("status") or raw.get("market_status") or "OPEN").upper(),
        "period": _period_code(raw.get("period")),
        "line": raw.get("line"),
        "overtimeScope": raw.get("overtime_scope") or raw.get("overtimeScope"),
        "rulesScope": raw.get("rules_scope") or raw.get("rulesScope"),
        "selections": [
            {
                "selectionId": sel.get("selection_id")
                or sel.get("selectionId")
                or sel.get("source_selection_id"),
                "name": sel.get("name") or sel.get("display_label"),
                "canonicalOutcomeKey": _example_outcome_key(
                    canonical,
                    sel.get("canonical_outcome_key"),
                    sel.get("name") or sel.get("display_label"),
                ),
                "price": sel.get("price") or sel.get("decimal_odds"),
                "status": (sel.get("status") or sel.get("selection_status") or "ACTIVE").upper(),
                "line": sel.get("line"),
            }
            for sel in raw.get("selections", [])
            if isinstance(sel, dict)
        ],
    }


def _example_outcome_key(
    canonical_market_definition_id: object,
    explicit: object,
    display_label: object,
) -> str | None:
    if explicit is not None:
        return str(explicit)
    exact = {
        ("football-match-result-1x2", "Home"): "home",
        ("football-match-result-1x2", "Draw"): "draw",
        ("football-match-result-1x2", "Away"): "away",
        ("football-total-goals", "Over"): "over",
        ("football-total-goals", "Under"): "under",
        ("football-btts", "Yes"): "yes",
        ("football-btts", "No"): "no",
        ("basketball-match-winner-with-ot", "Home"): "home",
        ("basketball-match-winner-with-ot", "Away"): "away",
        ("basketball-total-points-with-ot", "Over"): "over",
        ("basketball-total-points-with-ot", "Under"): "under",
        ("basketball-spread-with-ot", "Home -3.5"): "home",
        ("basketball-spread-with-ot", "Away +3.5"): "away",
        ("tennis-match-winner", "Elena Marquez"): "home",
        ("tennis-match-winner", "Sofia Lindqvist"): "away",
    }
    return exact.get((str(canonical_market_definition_id), str(display_label)))


def _sport_code(value: object) -> str:
    mapping = {
        "football": "FOOT",
        "basketball": "BASK",
        "tennis": "TENN",
    }
    if isinstance(value, str):
        if value.upper() in {"FOOT", "BASK", "TENN"}:
            return value.upper()
        mapped = mapping.get(value.lower())
        if mapped is not None:
            return mapped
    return "FOOT"


EXAMPLE_BETANO_SYNTHETIC_PROFILE = ExampleBetanoSyntheticExtractionProfile()
