"""Betclic example/synthetic extraction profile tests (not provider-native)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sports_analytics.sources.betclic.catalog import ADAPTER_VERSION, PROVIDER_ID
from sports_analytics.sources.bookmaker_extraction.adapter_contract import ADAPTER_CONTRACT_SCHEMA
from sports_analytics.sources.bookmaker_extraction.example_betclic import (
    EXAMPLE_BETCLIC_SYNTHETIC_PROFILE,
    ExampleBetclicSyntheticExtractionProfile,
)
from sports_analytics.sources.bookmaker_extraction.pipeline import apply_extraction_profile
from sports_analytics.sources.browser.contracts import BrowserAcquisitionResult, BrowserMode
from sports_analytics.sources.raw_capture import BookmakerRawCaptureStore

OBSERVED = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "betclic" / "football.json"


def _browser() -> BrowserAcquisitionResult:
    return BrowserAcquisitionResult(
        provider_id=PROVIDER_ID,
        sport="football",
        acquisition_cycle_id="cycle-example",
        observed_at_utc=OBSERVED,
        browser_mode=BrowserMode.VISIBLE,
        pages=(),
        responses=(),
        diagnostics=(),
        block_reason=None,
        warnings=(),
    )


def _stored_capture(tmp_path: Path, *, content: str):
    store = BookmakerRawCaptureStore(tmp_path)
    return store.store_text(
        source_name=PROVIDER_ID,
        capture_kind="provider-json",
        content=content,
        retrieved_at=OBSERVED,
        extension="json",
        source_url="https://www.betclic.pt/test",
    )


def test_example_profile_positive_translation(tmp_path: Path) -> None:
    profile = ExampleBetclicSyntheticExtractionProfile()
    profile.raw_directory = tmp_path
    payload = FIXTURE.read_text(encoding="utf-8")
    result = profile.extract(
        browser_result=_browser(),
        captures=(_stored_capture(tmp_path, content=payload),),
    )
    assert result.verified is False
    assert len(result.adapter_contract_payloads) == 1
    assert result.adapter_contract_payloads[0]["schema"] == ADAPTER_CONTRACT_SCHEMA


def test_example_profile_rejects_unknown_schema(tmp_path: Path) -> None:
    profile = ExampleBetclicSyntheticExtractionProfile()
    profile.raw_directory = tmp_path
    bad = json.dumps({"schema": "unknown-envelope-v9", "events": []})
    result = profile.extract(
        browser_result=_browser(),
        captures=(_stored_capture(tmp_path, content=bad),),
    )
    assert "unknown-schema" in result.drift_codes


def test_unverified_profile_marks_drift_on_bundle(tmp_path: Path) -> None:
    profile = ExampleBetclicSyntheticExtractionProfile()
    profile.raw_directory = tmp_path
    payload = FIXTURE.read_text(encoding="utf-8")
    capture = _stored_capture(tmp_path, content=payload)
    bundle = apply_extraction_profile(
        profile=profile,
        browser_result=_browser(),
        captures=(capture,),
        adapter_version=ADAPTER_VERSION,
        parser_version="betclic-pt-parser-v1",
        observed_at_utc=OBSERVED,
        sport="football",
    )
    assert "unverified-extraction-profile" in bundle.drift_codes
    assert EXAMPLE_BETCLIC_SYNTHETIC_PROFILE.verified is False
