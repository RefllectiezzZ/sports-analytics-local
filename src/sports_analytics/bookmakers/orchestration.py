"""Production bookmaker acquisition orchestration with explicit fallback."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sports_analytics.bookmakers.fallback import (
    CachedSnapshotReference,
    ProviderAttemptOutcome,
    ProviderFallbackDecision,
    resolve_provider_fallback,
)
from sports_analytics.bookmakers.loader import load_bookmaker_snapshot
from sports_analytics.bookmakers.service import BookmakerIngestionService
from sports_analytics.bookmakers.types import (
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
    BookmakerIngestionResult,
    FailureClassification,
    SelectionMode,
)
from sports_analytics.bookmakers.window import AcquisitionWindow
from sports_analytics.core.exceptions import PermanentJobError, RetryableJobError
from sports_analytics.core.settings import BookmakersSettings
from sports_analytics.data.codec import parse_utc_timestamp
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.bookmakers import BookmakerRepository
from sports_analytics.data.types import JsonValue, normalize_uuid

_PROVIDER_CYCLE_SUFFIX: dict[str, str] = {
    PROVIDER_BETANO_PT: "betano",
    PROVIDER_BETCLIC_PT: "betclic",
}


def provider_sub_cycle_id(logical_cycle_id: str, provider_id: str) -> str:
    """Return the deterministic provider sub-attempt cycle identity."""
    suffix = _PROVIDER_CYCLE_SUFFIX.get(provider_id)
    if suffix is None:
        msg = f"unsupported provider for sub-cycle identity: {provider_id}"
        raise PermanentJobError(msg)
    return f"{logical_cycle_id}-{suffix}"


@dataclass(frozen=True, slots=True)
class CycleLifecycleTimestamps:
    """Exact autonomous cycle lifecycle timestamps."""

    scheduled_for_utc: datetime | None
    enqueued_at_utc: datetime | None
    acquisition_started_at_utc: datetime
    provider_response_observed_at_utc: datetime | None
    acquisition_finished_at_utc: datetime


@dataclass(frozen=True, slots=True)
class ProviderSubAttempt:
    """One provider acquisition sub-attempt within a logical autonomous cycle."""

    provider_id: str
    acquisition_cycle_id: str
    result: BookmakerIngestionResult | None
    outcome: ProviderAttemptOutcome
    skipped_cooldown: bool = False


@dataclass(frozen=True, slots=True)
class OrchestratedAcquisitionResult:
    """Result of one autonomous preferred/fallback acquisition cycle."""

    sport: str
    acquisition_cycle_id: str
    fallback_decision: ProviderFallbackDecision
    betano_result: BookmakerIngestionResult | None
    betclic_result: BookmakerIngestionResult | None
    selected_result: BookmakerIngestionResult | None
    provider_sub_attempts: tuple[ProviderSubAttempt, ...] = ()
    lifecycle: CycleLifecycleTimestamps | None = None


class BookmakerAcquisitionOrchestrator:
    """Coordinate Betano preferred / Betclic comparison acquisition cycles."""

    def __init__(
        self,
        *,
        service: BookmakerIngestionService,
        bookmakers: BookmakersSettings,
        database_path: Path,
        raw_directory: Path,
        snapshots_directory: Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._service = service
        self._bookmakers = bookmakers
        self._database_path = database_path
        self._raw_directory = raw_directory
        self._snapshots_directory = snapshots_directory
        self._clock = clock if clock is not None else (lambda: datetime.now(tz=UTC))

    def run_autonomous_cycle(
        self,
        *,
        sport: str,
        acquisition_cycle_id: str | None = None,
        observed_at_utc: datetime | None = None,
        scheduled_for_utc: datetime | None = None,
        enqueued_at_utc: datetime | None = None,
        actor: str = "bookmaker-orchestrator",
        attempt_number: int = 1,
        maximum_attempts: int = 2,
        acquisition_window: AcquisitionWindow | None = None,
    ) -> OrchestratedAcquisitionResult:
        """Acquire providers per selection mode with independent sub-attempts."""
        cycle_id = (
            acquisition_cycle_id if acquisition_cycle_id is not None else normalize_uuid(None)
        )
        started_at = self._normalize_utc(self._clock())
        scheduled = (
            self._normalize_utc(scheduled_for_utc) if scheduled_for_utc is not None else None
        )
        enqueued = self._normalize_utc(enqueued_at_utc) if enqueued_at_utc is not None else None
        # Provider response observation time is captured by the browser session /
        # ingestion path, never substituted with the scheduled slot identity.
        observed_at = self._normalize_utc(observed_at_utc) if observed_at_utc is not None else None
        providers = _providers_for_selection_mode(self._bookmakers)
        sub_attempts: list[ProviderSubAttempt] = []
        retryable_providers: list[str] = []
        betano_result: BookmakerIngestionResult | None = None
        betclic_result: BookmakerIngestionResult | None = None
        provider_observed_at: datetime | None = None

        for provider_id in providers:
            sub_cycle_id = provider_sub_cycle_id(cycle_id, provider_id)
            if not self._provider_eligible(provider_id=provider_id, sport=sport, now=started_at):
                outcome = ProviderAttemptOutcome(
                    provider_id=provider_id,
                    success=False,
                    failure_classification=FailureClassification.BLOCKED,
                    block_or_failure_code="provider-cooldown",
                )
                sub_attempts.append(
                    ProviderSubAttempt(
                        provider_id=provider_id,
                        acquisition_cycle_id=sub_cycle_id,
                        result=None,
                        outcome=outcome,
                        skipped_cooldown=True,
                    )
                )
                continue
            try:
                result = self._ingest_provider(
                    provider_id=provider_id,
                    sport=sport,
                    acquisition_cycle_id=sub_cycle_id,
                    observed_at_utc=observed_at if observed_at is not None else started_at,
                    actor=actor,
                    attempt_number=attempt_number,
                    maximum_attempts=maximum_attempts,
                    acquisition_window=acquisition_window,
                )
            except RetryableJobError:
                outcome = ProviderAttemptOutcome(
                    provider_id=provider_id,
                    success=False,
                    failure_classification=FailureClassification.RETRYABLE,
                    block_or_failure_code="retryable-source-error",
                )
                sub_attempts.append(
                    ProviderSubAttempt(
                        provider_id=provider_id,
                        acquisition_cycle_id=sub_cycle_id,
                        result=None,
                        outcome=outcome,
                    )
                )
                if attempt_number < maximum_attempts:
                    retryable_providers.append(provider_id)
                continue
            outcome = _outcome_from_result(result, provider_id=provider_id)
            if result.observed_at_utc:
                try:
                    provider_observed_at = parse_utc_timestamp(result.observed_at_utc)
                except Exception:  # noqa: BLE001
                    provider_observed_at = provider_observed_at
            sub_attempts.append(
                ProviderSubAttempt(
                    provider_id=provider_id,
                    acquisition_cycle_id=sub_cycle_id,
                    result=result,
                    outcome=outcome,
                )
            )
            if provider_id == PROVIDER_BETANO_PT:
                betano_result = result
            elif provider_id == PROVIDER_BETCLIC_PT:
                betclic_result = result

        # Preserve successful providers while requesting a retry for unfinished
        # retryable providers. Successful provider sub-cycles are idempotent on
        # replay via acquisition_cycle_id (service returns the existing run).
        if retryable_providers and attempt_number < maximum_attempts:
            raise RetryableJobError(
                "retryable provider sub-attempts remain: " + ",".join(sorted(retryable_providers))
            )

        finished_at = self._normalize_utc(self._clock())
        lifecycle = CycleLifecycleTimestamps(
            scheduled_for_utc=scheduled,
            enqueued_at_utc=enqueued,
            acquisition_started_at_utc=started_at,
            provider_response_observed_at_utc=provider_observed_at,
            acquisition_finished_at_utc=finished_at,
        )

        betano_attempt = _attempt_for_provider(sub_attempts, PROVIDER_BETANO_PT)
        betclic_attempt = _attempt_for_provider(sub_attempts, PROVIDER_BETCLIC_PT)
        comparison_attempt = betclic_attempt if betclic_attempt is not None else None
        if betano_attempt is None:
            betano_attempt = ProviderAttemptOutcome(
                provider_id=PROVIDER_BETANO_PT,
                success=False,
                failure_classification=FailureClassification.PERMANENT,
                block_or_failure_code="not-attempted",
            )

        cached = self._select_verified_cached_snapshot_reference(sport=sport)
        decision = resolve_provider_fallback(
            preferred_attempt=betano_attempt,
            comparison_attempt=comparison_attempt,
            cached_snapshot=cached,
        )
        self._persist_fallback(decision)
        selected = None
        if decision.selected_provider == PROVIDER_BETANO_PT:
            selected = betano_result
        elif decision.selected_provider == PROVIDER_BETCLIC_PT:
            selected = betclic_result
        return OrchestratedAcquisitionResult(
            sport=sport,
            acquisition_cycle_id=cycle_id,
            fallback_decision=decision,
            betano_result=betano_result,
            betclic_result=betclic_result,
            selected_result=selected,
            provider_sub_attempts=tuple(sub_attempts),
            lifecycle=lifecycle,
        )

    def _provider_eligible(self, *, provider_id: str, sport: str, now: datetime) -> bool:
        with connect_database(self._database_path, read_only=True) as connection:
            status = BookmakerRepository(connection).get_provider_status(provider_id, sport)
        if status is None:
            return True
        if status.get("status") != "blocked":
            return True
        next_eligible_raw = status.get("next_eligible_at_utc")
        if not isinstance(next_eligible_raw, str) or not next_eligible_raw:
            return False
        return parse_utc_timestamp(next_eligible_raw) <= now

    def _ingest_provider(
        self,
        *,
        provider_id: str,
        sport: str,
        acquisition_cycle_id: str,
        observed_at_utc: datetime,
        actor: str,
        attempt_number: int,
        maximum_attempts: int,
        acquisition_window: AcquisitionWindow | None,
    ) -> BookmakerIngestionResult:
        try:
            return self._service.ingest(
                provider_id=provider_id,
                sport=sport,
                observed_at_utc=observed_at_utc,
                acquisition_cycle_id=acquisition_cycle_id,
                actor=actor,
                attempt_number=attempt_number,
                maximum_attempts=maximum_attempts,
                acquisition_window=acquisition_window,
            )
        except RetryableJobError:
            raise
        except PermanentJobError:
            return BookmakerIngestionResult(
                provider_id=provider_id,
                sport=sport,
                acquisition_cycle_id=acquisition_cycle_id,
                adapter_version="",
                status="failed",
                observed_at_utc="",
                snapshot_id=None,
                snapshot_reused=False,
                block_reason=None,
                failure_classification="permanent",
                events_observed=0,
                valid_quotes_observed=0,
                unresolved_events=0,
                rejected_markets=0,
                warnings=(),
                drift_codes=(),
            )

    def _select_verified_cached_snapshot_reference(
        self,
        *,
        sport: str,
    ) -> CachedSnapshotReference | None:
        candidates: list[CachedSnapshotReference] = []
        now = self._clock()
        for provider_id in (PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT):
            reference = self._verified_cached_snapshot_reference(
                provider_id=provider_id,
                sport=sport,
                now=now,
            )
            if reference is not None:
                candidates.append(reference)
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.observed_at_utc)

    def _verified_cached_snapshot_reference(
        self,
        *,
        provider_id: str,
        sport: str,
        now: datetime,
    ) -> CachedSnapshotReference | None:
        with connect_database(self._database_path, read_only=True) as connection:
            repo = BookmakerRepository(connection)
            status = repo.get_provider_status(provider_id, sport)
            registration = None
            snap_id = None
            if status is not None:
                raw_snap = status.get("last_valid_snapshot_id")
                if isinstance(raw_snap, str) and raw_snap:
                    snap_id = raw_snap
            if snap_id is None:
                return None
            registration = repo.get_snapshot_registration(snap_id)
        if registration is None:
            return None
        observed_raw = registration.get("observed_at_utc")
        if observed_raw is None:
            observed_raw = status.get("last_successful_at_utc") if status is not None else None
        if not isinstance(observed_raw, str) or not observed_raw:
            return None
        observed_at = parse_utc_timestamp(observed_raw)
        if observed_at > now:
            return None
        age_seconds = max(0, int((now - observed_at).total_seconds()))
        with connect_database(self._database_path, read_only=True) as connection:
            loaded = load_bookmaker_snapshot(
                database_connection=connection,
                snapshots_directory=self._snapshots_directory,
                raw_directory=self._raw_directory,
                snapshot_id=snap_id,
            )
        if loaded.provider_id != provider_id or loaded.sport != sport:
            return None
        return CachedSnapshotReference(
            provider_id=provider_id,
            snapshot_id=snap_id,
            observed_at_utc=observed_at,
            age_seconds=age_seconds,
            is_current=False,
        )

    def _normalize_utc(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _persist_fallback(self, decision: ProviderFallbackDecision) -> None:
        attempted: JsonValue = [
            {
                "provider_id": provider_id,
                "failure_classification": classification.value,
            }
            for provider_id, classification in decision.failure_classifications
        ]
        if decision.cached_used:
            attempted = {
                "providers": attempted,
                "cached_provider_id": decision.cached_provider_id,
                "cached_snapshot_id": decision.cached_snapshot_id,
                "cached_age_seconds": decision.cached_age_seconds,
            }
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                BookmakerRepository(connection).insert_fallback_decision(
                    preferred_provider=decision.preferred_provider,
                    selected_provider=decision.selected_provider,
                    cached_used=decision.cached_used,
                    cached_age_seconds=decision.cached_age_seconds,
                    reason_code=decision.reason_code.value,
                    attempted=attempted,
                    created_at=self._clock(),
                )


def _providers_for_selection_mode(bookmakers: BookmakersSettings) -> tuple[str, ...]:
    mode = SelectionMode(bookmakers.selection_mode)
    if mode is SelectionMode.BETANO:
        return (PROVIDER_BETANO_PT,) if bookmakers.betano.enabled else ()
    if mode is SelectionMode.BETCLIC:
        return (PROVIDER_BETCLIC_PT,) if bookmakers.betclic.enabled else ()
    providers: list[str] = []
    if bookmakers.betano.enabled:
        providers.append(PROVIDER_BETANO_PT)
    if bookmakers.betclic.enabled:
        providers.append(PROVIDER_BETCLIC_PT)
    return tuple(providers)


def _outcome_from_result(
    result: BookmakerIngestionResult | None,
    *,
    provider_id: str,
) -> ProviderAttemptOutcome:
    if result is None:
        return ProviderAttemptOutcome(
            provider_id=provider_id,
            success=False,
            failure_classification=FailureClassification.PERMANENT,
            block_or_failure_code="not-attempted",
        )
    if result.status == "succeeded":
        return ProviderAttemptOutcome(
            provider_id=provider_id,
            success=True,
            failure_classification=FailureClassification.NONE,
        )
    if result.status == "blocked":
        return ProviderAttemptOutcome(
            provider_id=provider_id,
            success=False,
            failure_classification=FailureClassification.BLOCKED,
            block_or_failure_code=result.block_reason,
        )
    if result.failure_classification == FailureClassification.RETRYABLE.value:
        return ProviderAttemptOutcome(
            provider_id=provider_id,
            success=False,
            failure_classification=FailureClassification.RETRYABLE,
            block_or_failure_code=result.failure_classification or result.status,
        )
    if result.status in {"partial", "drift-detected", "unavailable"}:
        return ProviderAttemptOutcome(
            provider_id=provider_id,
            success=False,
            failure_classification=FailureClassification.PERMANENT,
            block_or_failure_code=result.status,
        )
    return ProviderAttemptOutcome(
        provider_id=provider_id,
        success=False,
        failure_classification=FailureClassification.PERMANENT,
        block_or_failure_code=result.failure_classification or result.status,
    )


def _attempt_for_provider(
    sub_attempts: tuple[ProviderSubAttempt, ...] | list[ProviderSubAttempt],
    provider_id: str,
) -> ProviderAttemptOutcome | None:
    for item in sub_attempts:
        if item.provider_id == provider_id:
            return item.outcome
    return None


def _any_success(sub_attempts: tuple[ProviderSubAttempt, ...] | list[ProviderSubAttempt]) -> bool:
    return any(item.outcome.success for item in sub_attempts)
