"""Production extraction profile registry (defaults to unverified / disabled)."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from sports_analytics.bookmakers.navigation import (
    DisabledStageBNavigationCapability,
    StageBNavigationCapability,
)
from sports_analytics.sources.betano.catalog import PROVIDER_ID as BETANO_PROVIDER_ID
from sports_analytics.sources.betclic.catalog import PROVIDER_ID as BETCLIC_PROVIDER_ID
from sports_analytics.sources.bookmaker_extraction.betano_topeventsv2 import (
    BETANO_FOOTBALL_TOPEVENTSV2_PROFILE,
)
from sports_analytics.sources.bookmaker_extraction.contracts import ExtractionProfile

VERIFICATION_PROCEDURE: str = """
Local verification procedure for installing a verified extraction profile
-----------------------------------------------------------------------
1. Run ordinary headless-browser acquisition against the target provider sport
   route with ``bookmakers.browser_mode = headless`` and logging enabled.
2. Inspect ``raw/<provider-id>/`` captures written during the cycle. Identify the
   sanitized JSON response shape(s) that contain pre-match fixtures and odds.
3. Redact credentials, cookies, account identifiers, and absolute private paths.
4. Implement a provider-specific ``ExtractionProfile`` that maps the observed
   sanitized shape into ``bookmaker-adapter-contract-v1`` without inventing field
   names that were not present in the observed payload.
5. Add positive and negative unit tests against the redacted fixture captures.
6. Register the profile in ``get_verified_extraction_profile`` for that provider
   only after tests pass and a human reviewer confirms the mapping against live
   evidence captured in step 1–2.
7. Re-run the full bookmaker unit and integration suites plus hosted CI.

Until step 6 completes, production adapters remain ``experimental-unverified`` and
admission rejects cycles with ``no-verified-extraction-profile``.

Betano football ``topEventsV2`` was registered after sanitized fixture recognition,
drift fail-closed tests, and snapshot publish/reload verification. Betclic remains
unregistered pending event/odds discovery.
""".strip()


_VERIFIED_PROFILES: dict[tuple[str, str], ExtractionProfile] = {
    (BETANO_PROVIDER_ID, "football"): BETANO_FOOTBALL_TOPEVENTSV2_PROFILE,
}


@dataclass(frozen=True, slots=True)
class ProviderSportCapability:
    """Exact evidence-gated provider/sport capability without fallback."""

    provider_id: str
    sport: str
    reviewed_route_id: str | None
    stage_a_profile_id: str | None
    stage_a_profile_version: str | None
    stage_b_enabled: bool
    detail_response_profile_id: str | None
    detail_response_profile_version: str | None
    native_parser_capability: str
    completeness_mechanism: str
    reviewed_canonical_mapping_set: tuple[str, ...]
    evidence_classification: str
    operational_classification: str

    def as_safe_dict(self) -> dict[str, object]:
        """Return a deterministic operator-safe representation."""
        return asdict(self)


_CAPABILITIES: dict[tuple[str, str], ProviderSportCapability] = (
    {
        (BETANO_PROVIDER_ID, "football"): ProviderSportCapability(
            provider_id=BETANO_PROVIDER_ID,
            sport="football",
            reviewed_route_id="football-prematch",
            stage_a_profile_id=BETANO_FOOTBALL_TOPEVENTSV2_PROFILE.profile_id,
            stage_a_profile_version=BETANO_FOOTBALL_TOPEVENTSV2_PROFILE.schema_version,
            stage_b_enabled=False,
            detail_response_profile_id=None,
            detail_response_profile_version=None,
            native_parser_capability="reviewed-landing-inventory-only",
            completeness_mechanism="unknown-landing-inventory",
            reviewed_canonical_mapping_set=(
                "football-btts",
                "football-match-result-1x2",
                "football-total-goals",
            ),
            evidence_classification="reviewed-non-exhaustive-landing-inventory",
            operational_classification="Stage-A-only",
        ),
    }
    | {
        (BETANO_PROVIDER_ID, sport): ProviderSportCapability(
            provider_id=BETANO_PROVIDER_ID,
            sport=sport,
            reviewed_route_id=f"{sport}-prematch",
            stage_a_profile_id=None,
            stage_a_profile_version=None,
            stage_b_enabled=False,
            detail_response_profile_id=None,
            detail_response_profile_version=None,
            native_parser_capability="unverified",
            completeness_mechanism="none",
            reviewed_canonical_mapping_set=(),
            evidence_classification="no-verified-profile",
            operational_classification="unsupported/unverified",
        )
        for sport in ("basketball", "tennis")
    }
    | {
        (BETCLIC_PROVIDER_ID, sport): ProviderSportCapability(
            provider_id=BETCLIC_PROVIDER_ID,
            sport=sport,
            reviewed_route_id=f"{sport}-prematch",
            stage_a_profile_id=None,
            stage_a_profile_version=None,
            stage_b_enabled=False,
            detail_response_profile_id=None,
            detail_response_profile_version=None,
            native_parser_capability="unverified",
            completeness_mechanism="none",
            reviewed_canonical_mapping_set=(),
            evidence_classification="streaming-transport-verified-profile-unverified",
            operational_classification="unsupported/unverified",
        )
        for sport in ("football", "basketball", "tennis")
    }
)


def get_verified_extraction_profile(
    provider_id: str,
    sport: str,
) -> ExtractionProfile | None:
    """Return only the exact verified provider/sport profile."""
    return _VERIFIED_PROFILES.get((provider_id, sport))


def get_provider_sport_capability(
    provider_id: str,
    sport: str,
) -> ProviderSportCapability:
    """Return one exact capability or an explicit unsupported record."""
    capability = _CAPABILITIES.get((provider_id, sport))
    if capability is not None:
        return capability
    return ProviderSportCapability(
        provider_id=provider_id,
        sport=sport,
        reviewed_route_id=None,
        stage_a_profile_id=None,
        stage_a_profile_version=None,
        stage_b_enabled=False,
        detail_response_profile_id=None,
        detail_response_profile_version=None,
        native_parser_capability="unverified",
        completeness_mechanism="none",
        reviewed_canonical_mapping_set=(),
        evidence_classification="unsupported-exact-tuple",
        operational_classification="unsupported/unverified",
    )


def list_provider_sport_capabilities() -> tuple[ProviderSportCapability, ...]:
    """List the six registered exact tuples in deterministic order."""
    return tuple(_CAPABILITIES[key] for key in sorted(_CAPABILITIES))


def get_stage_b_navigation_capability(
    provider_id: str,
    sport: str,
) -> StageBNavigationCapability:
    """Return an exact disabled-by-default Stage-B capability.

    No current provider/sport has reviewed event-detail navigation evidence.
    """
    capability = get_provider_sport_capability(provider_id, sport)
    if capability.stage_b_enabled:
        msg = "enabled Stage-B capability is not installed"
        raise RuntimeError(msg)
    return DisabledStageBNavigationCapability(provider_id=provider_id, sport=sport)
