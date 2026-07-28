"""Native Betano parser tests separate from synthetic fixtures."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sports_analytics.sources.betano.catalog import ADAPTER_VERSION, PROVIDER_ID
from sports_analytics.sources.betano.native_parser import parse_betano_native_payloads
from sports_analytics.sources.betano.synthetic import parse_betano_synthetic_payloads

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "betano" / "native"
OBSERVED = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def test_native_football_fixture_parses() -> None:
    payload = json.loads((FIXTURES / "football-offering.json").read_text(encoding="utf-8"))
    bundle = parse_betano_native_payloads(
        [payload],
        provider_id=PROVIDER_ID,
        adapter_version=ADAPTER_VERSION,
        acquisition_cycle_id="cycle-native",
        observed_at_utc=OBSERVED,
        sport="football",
    )
    assert len(bundle.events) == 1
    assert bundle.events[0].markets[0].canonical_market_definition_id == "football-match-result-1x2"


def test_production_parser_rejects_synthetic_envelope() -> None:
    synthetic = json.loads(
        (Path(__file__).resolve().parents[3] / "fixtures" / "betano" / "football.json").read_text(
            encoding="utf-8"
        )
    )
    bundle = parse_betano_native_payloads(
        [synthetic],
        provider_id=PROVIDER_ID,
        adapter_version=ADAPTER_VERSION,
        acquisition_cycle_id="cycle-reject",
        observed_at_utc=OBSERVED,
        sport="football",
    )
    assert bundle.events == ()
    assert "synthetic-schema-rejected" in bundle.drift_codes


def test_synthetic_adapter_still_parses_fixture_bundle() -> None:
    synthetic = json.loads(
        (Path(__file__).resolve().parents[3] / "fixtures" / "betano" / "football.json").read_text(
            encoding="utf-8"
        )
    )
    bundle = parse_betano_synthetic_payloads(
        [synthetic],
        provider_id=PROVIDER_ID,
        adapter_version=ADAPTER_VERSION,
        acquisition_cycle_id="cycle-synthetic",
        observed_at_utc=OBSERVED,
        sport="football",
    )
    assert len(bundle.events) == 1
