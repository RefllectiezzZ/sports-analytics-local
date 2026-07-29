"""Production extraction profile registry (defaults to unverified / disabled)."""

from __future__ import annotations

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

_CURRENT_PROVIDER_SPORTS = frozenset(
    (provider_id, sport)
    for provider_id in (BETANO_PROVIDER_ID, BETCLIC_PROVIDER_ID)
    for sport in ("football", "basketball", "tennis")
)


def get_verified_extraction_profile(
    provider_id: str,
    sport: str | None = None,
) -> ExtractionProfile | None:
    """Return the exact verified provider/sport profile.

    ``sport=None`` is retained only for compatibility with PR #11 callers and
    returns a profile when the provider has exactly one registered sport. New
    acquisition code always supplies the sport.
    """
    if sport is not None:
        return _VERIFIED_PROFILES.get((provider_id, sport))
    matches = [
        profile
        for (registered_provider, _), profile in _VERIFIED_PROFILES.items()
        if registered_provider == provider_id
    ]
    return matches[0] if len(matches) == 1 else None


def get_stage_b_navigation_capability(
    provider_id: str,
    sport: str,
) -> StageBNavigationCapability:
    """Return an exact disabled-by-default Stage-B capability.

    No current provider/sport has reviewed event-detail navigation evidence.
    """
    if (provider_id, sport) not in _CURRENT_PROVIDER_SPORTS:
        return DisabledStageBNavigationCapability(provider_id=provider_id, sport=sport)
    return DisabledStageBNavigationCapability(provider_id=provider_id, sport=sport)
