"""Exact provider/sport capability registry tests."""

from __future__ import annotations

import pytest

from sports_analytics.sources.bookmaker_extraction.registry import (
    get_provider_sport_capability,
    get_stage_b_navigation_capability,
    get_verified_extraction_profile,
    list_provider_sport_capabilities,
)


def test_registry_lists_exact_six_provider_sport_tuples() -> None:
    capabilities = list_provider_sport_capabilities()
    assert tuple((item.provider_id, item.sport) for item in capabilities) == (
        ("betano-pt", "basketball"),
        ("betano-pt", "football"),
        ("betano-pt", "tennis"),
        ("betclic-pt", "basketball"),
        ("betclic-pt", "football"),
        ("betclic-pt", "tennis"),
    )


def test_profile_lookup_requires_exact_provider_and_sport() -> None:
    assert get_verified_extraction_profile("betano-pt", "football") is not None
    for provider_id, sport in (
        ("betano-pt", "basketball"),
        ("betano-pt", "tennis"),
        ("betclic-pt", "football"),
        ("betclic-pt", "basketball"),
        ("betclic-pt", "tennis"),
        ("unknown-pt", "football"),
        ("betano-pt", "unknown"),
    ):
        assert get_verified_extraction_profile(provider_id, sport) is None


def test_provider_only_profile_fallback_is_not_callable() -> None:
    with pytest.raises(TypeError):
        get_verified_extraction_profile("betano-pt")  # type: ignore[call-arg]


def test_betano_evidence_classifications_remain_unchanged() -> None:
    football = get_provider_sport_capability("betano-pt", "football")
    assert football.evidence_classification == "reviewed-non-exhaustive-landing-inventory"
    assert football.completeness_mechanism == "unknown-landing-inventory"
    assert football.stage_a_profile_id == "betano-pt-football-topeventsv2-v1"
    assert football.stage_b_enabled is False
    for sport in ("basketball", "tennis"):
        capability = get_provider_sport_capability("betano-pt", sport)
        assert capability.evidence_classification == "no-verified-profile"
        assert capability.stage_a_profile_id is None
        assert capability.stage_b_enabled is False


def test_betclic_sports_remain_independently_evidence_gated() -> None:
    capabilities = [
        get_provider_sport_capability("betclic-pt", sport)
        for sport in ("football", "basketball", "tennis")
    ]
    assert all(item.stage_a_profile_id is None for item in capabilities)
    assert all(item.detail_response_profile_id is None for item in capabilities)
    assert all(item.native_parser_capability == "unverified" for item in capabilities)
    assert [item.evidence_classification for item in capabilities] == [
        "streaming-transport-verified-profile-unverified",
        "streaming-transport-verified-profile-unverified",
        "streaming-transport-verified-profile-unverified",
    ]
    assert [item.operational_classification for item in capabilities] == [
        "unsupported/unverified",
        "unsupported/unverified",
        "unsupported/unverified",
    ]


def test_unsupported_exact_tuple_returns_explicit_disabled_record() -> None:
    capability = get_provider_sport_capability("unknown-pt", "volleyball")
    assert capability.reviewed_route_id is None
    assert capability.stage_a_profile_id is None
    assert capability.stage_b_enabled is False
    assert capability.evidence_classification == "unsupported-exact-tuple"
    assert capability.operational_classification == "unsupported/unverified"


@pytest.mark.parametrize(
    ("provider_id", "sport"),
    [
        ("betano-pt", "football"),
        ("betano-pt", "basketball"),
        ("betano-pt", "tennis"),
        ("betclic-pt", "football"),
        ("betclic-pt", "basketball"),
        ("betclic-pt", "tennis"),
    ],
)
def test_stage_b_is_disabled_for_every_exact_tuple(provider_id: str, sport: str) -> None:
    capability = get_stage_b_navigation_capability(provider_id, sport)
    assert capability.provider_id == provider_id
    assert capability.sport == sport
    assert capability.enabled is False
