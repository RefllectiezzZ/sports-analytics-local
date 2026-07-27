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
)
from sports_analytics.core.exceptions import PermanentJobError, RetryableJobError
from sports_analytics.core.settings import BookmakersSettings
from sports_analytics.data.codec import parse_utc_timestamp
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.bookmakers import BookmakerRepository
from sports_analytics.data.types import JsonValue, normalize_uuid


@dataclass(frozen=True, slots=True)
class OrchestratedAcquisitionResult:
    """Result of one autonomous preferred/fallback acquisition cycle."""

    sport: str
    acquisition_cycle_id: str
    fallback_decision: ProviderFallbackDecision
    betano_result: BookmakerIngestionResult | None
    betclic_result: BookmakerIngestionResult | None
    selected_result: BookmakerIngestionResult | None


class BookmakerAcquisitionOrchestrator:
    """Coordinate Betano preferred / Betclic fallback acquisition cycles."""

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
        actor: str = "bookmaker-orchestrator",
        attempt_number: int = 1,
        maximum_attempts: int = 2,
    ) -> OrchestratedAcquisitionResult:
        """Attempt Betano, then Betclic when policy permits, with explicit fallback."""
        cycle_id = (
            acquisition_cycle_id if acquisition_cycle_id is not None else normalize_uuid(None)
        )
        observed_at = (
            self._normalize_utc(observed_at_utc)
            if observed_at_utc is not None
            else self._normalize_utc(self._clock())
        )
        betano_result: BookmakerIngestionResult | None = None
        betclic_result: BookmakerIngestionResult | None = None

        if self._bookmakers.betano.enabled:
            betano_result = self._ingest_provider(
                provider_id=PROVIDER_BETANO_PT,
                sport=sport,
                acquisition_cycle_id=f"{cycle_id}-betano",
                observed_at_utc=observed_at,
                actor=actor,
                attempt_number=attempt_number,
                maximum_attempts=maximum_attempts,
            )

        betano_attempt = _outcome_from_result(betano_result, provider_id=PROVIDER_BETANO_PT)
        comparison_attempt: ProviderAttemptOutcome | None = None

        if betano_attempt.success:
            decision = resolve_provider_fallback(
                preferred_attempt=betano_attempt,
                comparison_attempt=None,
            )
            self._persist_fallback(decision)
            return OrchestratedAcquisitionResult(
                sport=sport,
                acquisition_cycle_id=cycle_id,
                fallback_decision=decision,
                betano_result=betano_result,
                betclic_result=None,
                selected_result=betano_result,
            )

        if (
            betano_attempt.failure_classification is FailureClassification.RETRYABLE
            and attempt_number < maximum_attempts
        ):
            raise RetryableJobError(
                betano_attempt.block_or_failure_code or "retryable-betano-failure"
            )

        if self._bookmakers.betclic.enabled and _should_attempt_fallback(betano_attempt):
            betclic_result = self._ingest_provider(
                provider_id=PROVIDER_BETCLIC_PT,
                sport=sport,
                acquisition_cycle_id=f"{cycle_id}-betclic",
                observed_at_utc=observed_at,
                actor=actor,
                attempt_number=attempt_number,
                maximum_attempts=maximum_attempts,
            )
            comparison_attempt = _outcome_from_result(
                betclic_result,
                provider_id=PROVIDER_BETCLIC_PT,
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
        )

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


def _should_attempt_fallback(attempt: ProviderAttemptOutcome) -> bool:
    if attempt.success:
        return False
    if attempt.failure_classification is FailureClassification.BLOCKED:
        return True
    if attempt.failure_classification is FailureClassification.PERMANENT:
        return True
    return attempt.failure_classification is FailureClassification.RETRYABLE
