"""Competition identity normalization and optional-event admission."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime

import pytest

from sports_analytics.bookmakers.reconciliation import (
    competition_id_for_event,
    reconcile_bookmaker_bundles,
    sanitize_competition_events,
)
from sports_analytics.core.exceptions import NormalizationError
from sports_analytics.sources.bookmaker_contracts import (
    ProviderAcquisitionBundle,
    ProviderEventObservation,
    ProviderEventState,
    ProviderParticipantObservation,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _event(
    name: str | None,
    *,
    source_id: str = "source-42",
    event_id: str = "event-1",
) -> ProviderEventObservation:
    return ProviderEventObservation(
        source_event_id=event_id,
        source_competition_id=source_id,
        competition_display_name=name,
        sport="football",
        scheduled_start_utc=NOW,
        event_state=ProviderEventState.PRE_MATCH,
        participants=(
            ProviderParticipantObservation("side-a", "Synthetic Side A", "home"),
            ProviderParticipantObservation("side-b", "Synthetic Side B", "away"),
        ),
        markets=(),
        source_page_route_id="football-prematch",
    )


def _bundle(*events: ProviderEventObservation, provider: str = "betano-pt"):
    return ProviderAcquisitionBundle(
        provider_id=provider,
        adapter_version="synthetic-adapter-v1",
        acquisition_cycle_id=f"cycle-{provider}",
        observed_at_utc=NOW,
        sport="football",
        events=tuple(events),
        warnings=(),
        drift_codes=(),
        provenance=(),
    )


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("(Taça Sintética)", "taca-sintetica"),
        ("Líga D'Água / Norte", "liga-d-agua-norte"),
        ("...Copa///Nova!!!", "copa-nova"),
    ],
)
def test_display_name_punctuation_and_accents_are_safe(name: str, slug: str) -> None:
    identity = competition_id_for_event(_event(name), provider_id="betano-pt")
    assert identity.startswith(f"competition:{slug}:")
    assert len(identity) <= 128


def test_exact_semantic_name_matches_across_providers() -> None:
    event = _event("  LÍGA\tSintética  ")
    left = competition_id_for_event(event, provider_id="betano-pt")
    right = competition_id_for_event(event, provider_id="betclic-pt")
    assert left == right


def test_ascii_slug_collision_is_separated_by_unicode_digest() -> None:
    left = competition_id_for_event(_event("Liga ø"), provider_id="betano-pt")
    right = competition_id_for_event(_event("Liga"), provider_id="betano-pt")
    assert left != right


def test_long_name_is_bounded_and_deterministic() -> None:
    event = _event("Á" * 500)
    first = competition_id_for_event(event, provider_id="betano-pt")
    assert first == competition_id_for_event(event, provider_id="betclic-pt")
    assert len(first) <= 128
    assert re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,127}", first)


@pytest.mark.parametrize(
    "name",
    [
        "合成联赛",
        "Συνθετική Λίγκα",
        "Синтетическая Лига",
        "合成リーグ",
    ],
)
def test_non_latin_semantic_names_are_provider_independent(name: str) -> None:
    event = _event(name)
    left = competition_id_for_event(event, provider_id="betano-pt")
    right = competition_id_for_event(event, provider_id="betclic-pt")
    assert left == right
    assert left.startswith("competition:unicode:")
    assert re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,127}", left)


def test_composed_and_decomposed_unicode_are_identical() -> None:
    composed = "Taça Sintética"
    decomposed = unicodedata.normalize("NFD", composed)
    assert competition_id_for_event(
        _event(composed),
        provider_id="betano-pt",
    ) == competition_id_for_event(
        _event(decomposed),
        provider_id="betclic-pt",
    )


def test_provider_scoped_fallbacks_do_not_collide() -> None:
    event = _event(None, source_id="42")
    left = competition_id_for_event(event, provider_id="betano-pt")
    right = competition_id_for_event(event, provider_id="betclic-pt")
    assert left != right
    assert left.startswith("provider-competition:betano-pt:")
    assert right.startswith("provider-competition:betclic-pt:")


def test_provider_fallback_is_used_only_without_usable_display_name() -> None:
    semantic = competition_id_for_event(
        _event("合成联赛", source_id="42"),
        provider_id="betano-pt",
    )
    fallback = competition_id_for_event(
        _event(None, source_id="42"),
        provider_id="betano-pt",
    )
    assert semantic.startswith("competition:unicode:")
    assert fallback.startswith("provider-competition:")


def test_long_provider_fallback_is_bounded() -> None:
    identity = competition_id_for_event(
        _event(None, source_id="source-" + ("x" * 500)),
        provider_id="betano-pt",
    )
    assert len(identity) <= 128


def test_only_punctuation_identity_is_rejected() -> None:
    with pytest.raises(NormalizationError, match="safe normalized representation"):
        competition_id_for_event(
            _event("...", source_id="///"),
            provider_id="betano-pt",
        )


def test_control_characters_remain_rejected() -> None:
    with pytest.raises(NormalizationError, match="control"):
        competition_id_for_event(
            _event("Synthetic\u0000League"),
            provider_id="betano-pt",
        )


def test_malformed_optional_event_preserves_valid_event_and_records_finding() -> None:
    valid = _event("(Copa Sintética)", event_id="valid-event")
    malformed = _event("...", source_id="///", event_id="bad-event")
    sanitized = sanitize_competition_events((_bundle(valid, malformed),))[0]
    assert [event.source_event_id for event in sanitized.events] == ["valid-event"]
    assert "competition-identity-rejected" in sanitized.drift_codes
    assert sanitized.warnings[0].source_path == "events.bad-event"
    reconciled = reconcile_bookmaker_bundles((sanitized,))
    assert len(reconciled.event_candidates) == 1


def test_zero_valid_events_admits_no_candidates() -> None:
    malformed = _event("...", source_id="///", event_id="bad-event")
    sanitized = sanitize_competition_events((_bundle(malformed),))[0]
    assert sanitized.events == ()
    assert reconcile_bookmaker_bundles((sanitized,)).event_candidates == ()
