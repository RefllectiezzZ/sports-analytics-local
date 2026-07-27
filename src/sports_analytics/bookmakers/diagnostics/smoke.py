"""Provider smoke tests using verified extraction profiles."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sports_analytics.bookmakers.diagnostics.probe import (
    ProbeResult,
    collect_probe_from_acquisition,
)
from sports_analytics.bookmakers.diagnostics.redaction import redact_structure
from sports_analytics.bookmakers.loader import load_verified_bookmaker_quotes
from sports_analytics.bookmakers.markets import DEFINITION_FOOTBALL_MATCH_RESULT_1X2
from sports_analytics.bookmakers.types import PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT
from sports_analytics.core.exceptions import ConfigurationError
from sports_analytics.data.database import connect_database
from sports_analytics.data.migrations import ensure_database_ready
from sports_analytics.sources.betano.adapter import acquire_betano_current_odds
from sports_analytics.sources.betclic.adapter import acquire_betclic_current_odds
from sports_analytics.sources.bookmaker_catalog import SUPPORTED_BOOKMAKER_SPORTS
from sports_analytics.sources.bookmaker_extraction.registry import get_verified_extraction_profile
from sports_analytics.sources.browser.contracts import BrowserMode
from sports_analytics.sources.browser.playwright_runtime import PlaywrightBrowserSession

_MIN_EVENTS = 2
_MIN_MARKETS_PER_EVENT = 1


@dataclass(frozen=True, slots=True)
class SmokeCycleResult:
    """Outcome of one smoke-test acquisition cycle."""

    cycle_number: int
    events_extracted: int
    markets_with_odds: int
    snapshot_id: str | None
    snapshot_verified: bool
    snapshot_reused: bool


@dataclass(frozen=True, slots=True)
class SmokeResult:
    """Outcome of a bounded provider smoke test."""

    provider: str
    sport: str
    succeeded: bool
    failure_reason: str | None
    profile_id: str | None
    profile_verified: bool
    cycles: tuple[SmokeCycleResult, ...]
    diagnostic_relative_path: str
    acceptance_summary: dict[str, Any]


def smoke_bookmaker(
    *,
    provider_id: str,
    sport: str,
    duration_seconds: int = 60,
    diagnostic_directory: str | Path | None = None,
    database_path: Path | None = None,
    raw_directory: Path | None = None,
    snapshots_directory: Path | None = None,
    session: PlaywrightBrowserSession | None = None,
    extraction_profile: Any | None = None,
    clock: Any | None = None,
) -> SmokeResult:
    """Run a bounded visible smoke test for one provider sport surface."""
    if provider_id not in {PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT}:
        msg = f"unsupported smoke provider: {provider_id}"
        raise ConfigurationError(msg)
    if sport not in SUPPORTED_BOOKMAKER_SPORTS:
        msg = f"unsupported smoke sport: {sport}"
        raise ConfigurationError(msg)
    profile = extraction_profile or get_verified_extraction_profile(provider_id)
    profile_id = None if profile is None else profile.profile_id
    profile_verified = False if profile is None else bool(profile.verified)
    if profile is None or not profile.verified:
        return _failed_smoke(
            provider_id=provider_id,
            sport=sport,
            reason="no-verified-extraction-profile",
            profile_id=profile_id,
            profile_verified=profile_verified,
            diagnostic_directory=diagnostic_directory,
        )
    base = Path.cwd()
    db_path = database_path or base / "storage" / "operational.sqlite3"
    raw_dir = raw_directory or base / "storage" / "raw"
    snap_dir = snapshots_directory or base / "storage" / "snapshots"
    ensure_database_ready(db_path)
    raw_dir.mkdir(parents=True, exist_ok=True)
    snap_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(tz=UTC) if clock is None else clock()
    browser_session = session or PlaywrightBrowserSession(clock=(lambda: now))
    cycle_id = f"smoke-{provider_id}-{sport}"
    acquire = (
        acquire_betano_current_odds
        if provider_id == PROVIDER_BETANO_PT
        else acquire_betclic_current_odds
    )
    browser_result, bundle, _captures = acquire(
        sport=sport,
        acquisition_cycle_id=cycle_id,
        observed_at_utc=now,
        raw_directory=raw_dir,
        browser_mode=BrowserMode.VISIBLE,
        session=browser_session,
        extraction_profile=profile,
    )
    if browser_result.block_reason is not None:
        return _failed_smoke(
            provider_id=provider_id,
            sport=sport,
            reason=f"blocked:{browser_result.block_reason.value}",
            profile_id=profile_id,
            profile_verified=profile_verified,
            diagnostic_directory=diagnostic_directory,
        )
    events = len(bundle.events)
    markets = sum(len(event.markets) for event in bundle.events)
    valid_selections = sum(
        1
        for event in bundle.events
        for market in event.markets
        for selection in market.selections
        if selection.decimal_odds is not None
    )
    if (
        events < _MIN_EVENTS
        or markets < _MIN_EVENTS * _MIN_MARKETS_PER_EVENT
        or valid_selections < 1
    ):
        return _failed_smoke(
            provider_id=provider_id,
            sport=sport,
            reason="zero-events-or-odds",
            profile_id=profile_id,
            profile_verified=profile_verified,
            diagnostic_directory=diagnostic_directory,
            probe=collect_probe_from_acquisition(
                provider_id=provider_id,
                sport=sport,
                acquisition=browser_result,
                duration_seconds=float(duration_seconds),
                diagnostic_directory=diagnostic_directory,
            ),
        )
    cycle = SmokeCycleResult(
        cycle_number=1,
        events_extracted=events,
        markets_with_odds=markets,
        snapshot_id=None,
        snapshot_verified=False,
        snapshot_reused=False,
    )
    summary = {
        "provider": provider_id,
        "sport": sport,
        "events_extracted": events,
        "markets_with_odds": markets,
        "valid_selections": valid_selections,
        "target_market": DEFINITION_FOOTBALL_MATCH_RESULT_1X2 if sport == "football" else None,
        "profile_id": profile_id,
        "profile_verified": profile_verified,
    }
    probe = collect_probe_from_acquisition(
        provider_id=provider_id,
        sport=sport,
        acquisition=browser_result,
        duration_seconds=float(duration_seconds),
        diagnostic_directory=diagnostic_directory,
    )
    artifact = _write_smoke_artifact(
        probe.diagnostic_relative_path,
        provider_id=provider_id,
        sport=sport,
        succeeded=True,
        failure_reason=None,
        profile_id=profile_id,
        profile_verified=profile_verified,
        cycles=(cycle,),
        acceptance_summary=summary,
        diagnostic_directory=diagnostic_directory,
    )
    return SmokeResult(
        provider=provider_id,
        sport=sport,
        succeeded=True,
        failure_reason=None,
        profile_id=profile_id,
        profile_verified=profile_verified,
        cycles=(cycle,),
        diagnostic_relative_path=artifact,
        acceptance_summary=redact_structure(summary),
    )


def evaluate_fake_session_smoke(
    *,
    provider_id: str,
    sport: str,
    events_extracted: int,
    markets_with_odds: int,
    profile_verified: bool,
    profile_id: str | None,
) -> SmokeResult:
    """Evaluate smoke criteria from injected test evidence without live browser."""
    succeeded = (
        profile_verified
        and events_extracted >= _MIN_EVENTS
        and markets_with_odds >= _MIN_MARKETS_PER_EVENT * _MIN_EVENTS
    )
    failure = None if succeeded else "zero-events-or-odds"
    cycle = SmokeCycleResult(
        cycle_number=1,
        events_extracted=events_extracted,
        markets_with_odds=markets_with_odds,
        snapshot_id="snap-test" if succeeded else None,
        snapshot_verified=succeeded,
        snapshot_reused=False,
    )
    summary = {
        "provider": provider_id,
        "sport": sport,
        "events_extracted": events_extracted,
        "markets_with_odds": markets_with_odds,
        "profile_id": profile_id,
        "profile_verified": profile_verified,
    }
    return SmokeResult(
        provider=provider_id,
        sport=sport,
        succeeded=succeeded,
        failure_reason=failure,
        profile_id=profile_id,
        profile_verified=profile_verified,
        cycles=(cycle,),
        diagnostic_relative_path="smoke-fake-session.json",
        acceptance_summary=redact_structure(summary),
    )


def verify_loaded_snapshot_quotes(
    *,
    database_path: Path,
    snapshots_directory: Path,
    raw_directory: Path,
    snapshot_id: str,
) -> bool:
    """Return whether strict snapshot verification succeeds."""
    with connect_database(database_path, read_only=True) as connection:
        loaded = load_verified_bookmaker_quotes(
            database_connection=connection,
            snapshots_directory=snapshots_directory,
            raw_directory=raw_directory,
            snapshot_id=snapshot_id,
        )
    return loaded.verified and loaded.quote_count >= 1


def _failed_smoke(
    *,
    provider_id: str,
    sport: str,
    reason: str,
    profile_id: str | None,
    profile_verified: bool,
    diagnostic_directory: str | Path | None,
    probe: ProbeResult | None = None,
) -> SmokeResult:
    summary = {
        "provider": provider_id,
        "sport": sport,
        "failure_reason": reason,
        "profile_id": profile_id,
        "profile_verified": profile_verified,
    }
    artifact = _write_smoke_artifact(
        probe.diagnostic_relative_path if probe is not None else "smoke-failed.json",
        provider_id=provider_id,
        sport=sport,
        succeeded=False,
        failure_reason=reason,
        profile_id=profile_id,
        profile_verified=profile_verified,
        cycles=(),
        acceptance_summary=summary,
        diagnostic_directory=diagnostic_directory,
    )
    return SmokeResult(
        provider=provider_id,
        sport=sport,
        succeeded=False,
        failure_reason=reason,
        profile_id=profile_id,
        profile_verified=profile_verified,
        cycles=(),
        diagnostic_relative_path=artifact,
        acceptance_summary=redact_structure(summary),
    )


def _write_smoke_artifact(
    probe_name: str,
    *,
    provider_id: str,
    sport: str,
    succeeded: bool,
    failure_reason: str | None,
    profile_id: str | None,
    profile_verified: bool,
    cycles: tuple[SmokeCycleResult, ...],
    acceptance_summary: dict[str, Any],
    diagnostic_directory: str | Path | None,
) -> str:
    from sports_analytics.bookmakers.diagnostics.paths import resolve_diagnostic_directory

    output_dir = resolve_diagnostic_directory(diagnostic_directory)
    filename = f"smoke-{provider_id}-{sport}.json"
    path = output_dir / filename
    payload = {
        "provider": provider_id,
        "sport": sport,
        "succeeded": succeeded,
        "failure_reason": failure_reason,
        "profile_id": profile_id,
        "profile_verified": profile_verified,
        "probe_artifact": probe_name,
        "cycles": [asdict(item) for item in cycles],
        "acceptance_summary": redact_structure(acceptance_summary),
    }
    path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    return filename
