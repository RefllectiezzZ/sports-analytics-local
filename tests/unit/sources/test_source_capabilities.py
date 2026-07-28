"""Source role and capability contract tests for the implemented source catalog.

Only adapters that exist are registered. These tests pin the capability surface so
callers cannot silently assume current-odds or fixture behaviour that no adapter
provides.
"""

from __future__ import annotations

import pytest

from sports_analytics.core.exceptions import PermanentSourceError
from sports_analytics.sources.catalog import (
    FOOTBALL_DATA_ADAPTER_VERSION,
    get_source_descriptor,
    list_source_descriptors,
    list_source_names,
    list_sources_with_capability,
)
from sports_analytics.sources.contracts import (
    SourceCapability,
    SourceRole,
    parse_source_capability,
    parse_source_role,
)
from sports_analytics.sources.football_data_co_uk.catalog import list_competitions
from sports_analytics.sources.types import SOURCE_FOOTBALL_DATA_CO_UK

HISTORICAL_CAPABILITIES = (
    SourceCapability.HISTORICAL_RESULTS,
    SourceCapability.HISTORICAL_STATISTICS,
    SourceCapability.HISTORICAL_ODDS,
)
CURRENT_CAPABILITIES = (
    SourceCapability.CURRENT_ODDS,
    SourceCapability.CURRENT_FIXTURES,
    SourceCapability.SETTLEMENT_RESULTS,
)


def test_source_descriptors_are_listed_in_deterministic_identifier_order() -> None:
    descriptors = list_source_descriptors()
    names = list_source_names()

    assert descriptors is list_source_descriptors()
    assert names == tuple(sorted(names))
    assert names == tuple(descriptor.source_id for descriptor in descriptors)
    assert SOURCE_FOOTBALL_DATA_CO_UK in names
    assert "betano-pt" in names
    assert "betclic-pt" in names


def test_football_data_declares_only_historical_capabilities() -> None:
    descriptor = get_source_descriptor(SOURCE_FOOTBALL_DATA_CO_UK)

    assert descriptor.capabilities == frozenset(HISTORICAL_CAPABILITIES)
    assert descriptor.capability_values == (
        "historical-odds",
        "historical-results",
        "historical-statistics",
    )
    assert descriptor.role is SourceRole.HISTORICAL_DATA
    assert descriptor.adapter_version == FOOTBALL_DATA_ADAPTER_VERSION
    for capability in HISTORICAL_CAPABILITIES:
        assert descriptor.has_capability(capability) is True
        descriptor.require_capability(capability)


@pytest.mark.parametrize("capability", CURRENT_CAPABILITIES)
def test_has_capability_is_false_for_valid_but_undeclared_capability(
    capability: SourceCapability,
) -> None:
    descriptor = get_source_descriptor(SOURCE_FOOTBALL_DATA_CO_UK)

    assert descriptor.has_capability(capability) is False
    assert descriptor.has_capability(capability.value) is False


def test_has_capability_rejects_unknown_capability_strings() -> None:
    descriptor = get_source_descriptor(SOURCE_FOOTBALL_DATA_CO_UK)

    with pytest.raises(PermanentSourceError, match="unknown source capability"):
        descriptor.has_capability("live-odds")


@pytest.mark.parametrize("capability", CURRENT_CAPABILITIES)
def test_require_capability_raises_for_undeclared_capability(
    capability: SourceCapability,
) -> None:
    descriptor = get_source_descriptor(SOURCE_FOOTBALL_DATA_CO_UK)

    with pytest.raises(PermanentSourceError, match="does not provide capability"):
        descriptor.require_capability(capability)


@pytest.mark.parametrize("source_id", ["betclic", "betano"])
def test_legacy_bookmaker_aliases_are_not_registered(source_id: str) -> None:
    with pytest.raises(PermanentSourceError, match="unsupported source_id"):
        get_source_descriptor(source_id)


def test_bookmaker_providers_provide_current_market_capabilities() -> None:
    current_odds = list_sources_with_capability(SourceCapability.CURRENT_ODDS)
    current_fixtures = list_sources_with_capability(SourceCapability.CURRENT_FIXTURES)
    assert {item.source_id for item in current_odds} == {"betano-pt", "betclic-pt"}
    assert {item.source_id for item in current_fixtures} == {"betano-pt", "betclic-pt"}
    assert list_sources_with_capability(SourceCapability.SETTLEMENT_RESULTS) == ()


def test_sources_with_historical_odds_capability_is_football_data() -> None:
    descriptors = list_sources_with_capability(SourceCapability.HISTORICAL_ODDS)

    assert tuple(descriptor.source_id for descriptor in descriptors) == (
        SOURCE_FOOTBALL_DATA_CO_UK,
    )


def test_football_data_supports_football_only() -> None:
    descriptor = get_source_descriptor(SOURCE_FOOTBALL_DATA_CO_UK)

    assert descriptor.supports_sport("football") is True
    assert descriptor.supports_sport("tennis") is False


def test_football_data_scopes_match_the_enabled_competition_catalog() -> None:
    descriptor = get_source_descriptor(SOURCE_FOOTBALL_DATA_CO_UK)

    assert descriptor.supported_scopes == tuple(
        entry.competition_id for entry in list_competitions()
    )
    assert descriptor.supported_scopes == ("eng-premier-league", "prt-primeira-liga")


@pytest.mark.parametrize("value", ["historical-odds", "current-odds", "settlement-results"])
def test_parse_source_capability_accepts_declared_values(value: str) -> None:
    assert parse_source_capability(value).value == value


@pytest.mark.parametrize("value", ["", "live-odds", "HISTORICAL-ODDS", " historical-odds"])
def test_parse_source_capability_rejects_unknown_values(value: str) -> None:
    with pytest.raises(PermanentSourceError, match="unknown source capability"):
        parse_source_capability(value)


@pytest.mark.parametrize("value", ["historical-data", "bookmaker", "results-feed"])
def test_parse_source_role_accepts_declared_values(value: str) -> None:
    assert parse_source_role(value).value == value


@pytest.mark.parametrize("value", ["", "scraper", "HISTORICAL-DATA", "historical_data"])
def test_parse_source_role_rejects_unknown_values(value: str) -> None:
    with pytest.raises(PermanentSourceError, match="unknown source role"):
        parse_source_role(value)
