"""Bookmaker current-odds ingestion service."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sports_analytics.bookmakers.normalization import (
    EMPTY_SOURCE_FILE_SHA256,
    normalize_bookmaker_bundles,
)
from sports_analytics.bookmakers.reconciliation import reconcile_bookmaker_bundles
from sports_analytics.bookmakers.snapshots import (
    build_bookmaker_source_version,
    publish_bookmaker_snapshot,
)
from sports_analytics.bookmakers.status import build_provider_status
from sports_analytics.bookmakers.types import (
    PROVIDER_BETANO_PT,
    PROVIDER_BETCLIC_PT,
    BookmakerIngestionResult,
    FailureClassification,
)
from sports_analytics.core.exceptions import (
    NormalizationError,
    ParserError,
    PermanentJobError,
    PermanentSourceError,
    RetryableJobError,
    RetryableSourceError,
    SnapshotBusyError,
    SnapshotIntegrityError,
)
from sports_analytics.core.settings import BookmakerProviderSettings, BookmakersSettings
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.bookmakers import BookmakerRepository
from sports_analytics.data.types import JsonValue, normalize_uuid, validate_identifier
from sports_analytics.snapshots.spec import RawArtifactReference
from sports_analytics.sources.betano.adapter import acquire_betano_current_odds
from sports_analytics.sources.betano.catalog import ADAPTER_VERSION as BETANO_ADAPTER_VERSION
from sports_analytics.sources.betclic.adapter import acquire_betclic_current_odds
from sports_analytics.sources.betclic.catalog import ADAPTER_VERSION as BETCLIC_ADAPTER_VERSION
from sports_analytics.sources.bookmaker_catalog import (
    SUPPORTED_BOOKMAKER_SPORTS,
    reject_forbidden_job_controls,
)
from sports_analytics.sources.bookmaker_contracts import ProviderAcquisitionBundle
from sports_analytics.sources.browser.contracts import BrowserAcquisitionResult, BrowserMode
from sports_analytics.sources.browser.playwright_runtime import BrowserSession


class BookmakerIngestionService:
    """Orchestrate one bookmaker current-odds acquisition cycle."""

    def __init__(
        self,
        *,
        database_path: Path,
        raw_directory: Path,
        snapshots_directory: Path,
        bookmakers: BookmakersSettings,
        clock: Callable[[], datetime] | None = None,
        session: BrowserSession | None = None,
    ) -> None:
        self._database_path = Path(database_path)
        self._raw_directory = Path(raw_directory)
        self._snapshots_directory = Path(snapshots_directory)
        self._bookmakers = bookmakers
        self._clock = clock if clock is not None else (lambda: datetime.now(tz=UTC))
        self._session = session

    def ingest(
        self,
        *,
        provider_id: str,
        sport: str,
        observed_at_utc: datetime | None = None,
        acquisition_cycle_id: str | None = None,
        actor: str = "bookmaker-ingestion-service",
        correlation_id: str | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> BookmakerIngestionResult:
        """Validate, acquire, normalize, publish, and persist operational state."""

        def _checkpoint() -> None:
            if checkpoint is not None:
                checkpoint()

        provider = validate_identifier(provider_id, field_name="provider_id")
        sport_code = validate_identifier(sport, field_name="sport")
        if provider not in {PROVIDER_BETANO_PT, PROVIDER_BETCLIC_PT}:
            msg = f"unsupported bookmaker provider: {provider}"
            raise PermanentJobError(msg)
        if sport_code not in SUPPORTED_BOOKMAKER_SPORTS:
            msg = f"unsupported bookmaker sport: {sport_code}"
            raise PermanentJobError(msg)
        if not self._bookmakers.enabled:
            msg = "bookmakers.enabled must be true to run acquisition"
            raise PermanentJobError(msg)
        provider_settings = self._provider_settings(provider)
        if not provider_settings.enabled:
            msg = f"bookmaker provider {provider} is disabled"
            raise PermanentJobError(msg)

        started_at = self._normalize_utc(self._clock())
        observed_at = (
            self._normalize_utc(observed_at_utc) if observed_at_utc is not None else started_at
        )
        cycle_id = (
            validate_identifier(acquisition_cycle_id, field_name="acquisition_cycle_id")
            if acquisition_cycle_id is not None
            else normalize_uuid(None)
        )
        adapter_version = self._adapter_version(provider)
        browser_mode = self._browser_mode()

        _checkpoint()
        try:
            browser_result, bundle, capture_paths = self._acquire(
                provider_id=provider,
                sport=sport_code,
                acquisition_cycle_id=cycle_id,
                observed_at_utc=observed_at,
                browser_mode=browser_mode,
            )
        except RetryableSourceError as exc:
            finished_at = self._normalize_utc(self._clock())
            self._persist_failure(
                provider_id=provider,
                sport=sport_code,
                acquisition_cycle_id=cycle_id,
                adapter_version=adapter_version,
                observed_at=observed_at,
                started_at=started_at,
                finished_at=finished_at,
                failure_classification="retryable-source-error",
                warnings=[],
            )
            raise RetryableJobError(str(exc)) from exc
        except PermanentSourceError as exc:
            finished_at = self._normalize_utc(self._clock())
            self._persist_failure(
                provider_id=provider,
                sport=sport_code,
                acquisition_cycle_id=cycle_id,
                adapter_version=adapter_version,
                observed_at=observed_at,
                started_at=started_at,
                finished_at=finished_at,
                failure_classification="permanent-source-error",
                warnings=[],
            )
            raise PermanentJobError(str(exc)) from exc

        _checkpoint()
        block_reason = browser_result.block_reason
        if block_reason is not None:
            finished_at = self._normalize_utc(self._clock())
            classification = block_reason.value
            warnings = list(browser_result.warnings)
            self._persist_blocked(
                provider_id=provider,
                sport=sport_code,
                acquisition_cycle_id=cycle_id,
                adapter_version=adapter_version,
                observed_at=observed_at,
                started_at=started_at,
                finished_at=finished_at,
                block_reason=classification,
                failure_classification=classification,
                warnings=warnings,
                blocked_cooldown_seconds=provider_settings.blocked_cooldown_seconds,
            )
            return BookmakerIngestionResult(
                provider_id=provider,
                sport=sport_code,
                acquisition_cycle_id=cycle_id,
                adapter_version=adapter_version,
                status="blocked",
                observed_at_utc=format_utc_timestamp(observed_at),
                snapshot_id=None,
                snapshot_reused=False,
                block_reason=classification,
                failure_classification=classification,
                events_observed=0,
                valid_quotes_observed=0,
                unresolved_events=0,
                rejected_markets=0,
                warnings=tuple(warnings),
                drift_codes=(),
            )

        _checkpoint()
        try:
            reconciliations = reconcile_bookmaker_bundles((bundle,))
            normalized = normalize_bookmaker_bundles(
                (bundle,),
                reconciliations=reconciliations,
            )
        except (NormalizationError, PermanentSourceError, ParserError) as exc:
            finished_at = self._normalize_utc(self._clock())
            self._persist_failure(
                provider_id=provider,
                sport=sport_code,
                acquisition_cycle_id=cycle_id,
                adapter_version=adapter_version,
                observed_at=observed_at,
                started_at=started_at,
                finished_at=finished_at,
                failure_classification="normalize-error",
                warnings=[warning.code for warning in bundle.warnings],
            )
            raise PermanentJobError(str(exc)) from exc

        _checkpoint()
        raw_sha = EMPTY_SOURCE_FILE_SHA256
        raw_artifact = RawArtifactReference(
            relative_path=(capture_paths[0] if capture_paths else "raw/bookmakers/empty.txt"),
            checksum_sha256=raw_sha,
            byte_count=0,
            encoding="utf-8",
        )
        source_version = build_bookmaker_source_version(
            sport_code=sport_code,
            acquisition_cycle_id=cycle_id,
            raw_sha256=raw_sha,
        )
        status_record = build_provider_status(
            provider_id=provider,
            adapter_version=adapter_version,
            observed_at_utc=observed_at,
            last_attempted_acquisition_utc=started_at,
            last_successful_acquisition_utc=None,
            last_valid_snapshot_id=None,
            snapshot_age_seconds=None,
            events_observed=len(bundle.events),
            valid_quotes_observed=len(normalized.market_quotes),
            unresolved_events=len(reconciliations.unresolved_event_reconciliations),
            rejected_markets=len(normalized.unknown_markets),
            warnings=tuple(sorted({warning.code for warning in bundle.warnings})),
            current_block_or_failure_classification=FailureClassification.NONE,
            next_eligible_attempt_utc=None,
            drift_detected=bool(bundle.drift_codes),
            acquisition_partial=bool(reconciliations.unresolved_event_reconciliations),
        )
        try:
            publication = publish_bookmaker_snapshot(
                database_path=self._database_path,
                snapshots_directory=self._snapshots_directory,
                sport_code=sport_code,
                source_version=source_version,
                source_observed_at_utc=observed_at,
                bundle=normalized,
                provider_statuses=(status_record,),
                raw_artifact=raw_artifact,
                actor=actor,
                correlation_id=correlation_id,
            )
        except SnapshotBusyError as exc:
            finished_at = self._normalize_utc(self._clock())
            self._persist_failure(
                provider_id=provider,
                sport=sport_code,
                acquisition_cycle_id=cycle_id,
                adapter_version=adapter_version,
                observed_at=observed_at,
                started_at=started_at,
                finished_at=finished_at,
                failure_classification="snapshot-busy",
                warnings=[warning.code for warning in bundle.warnings],
            )
            raise RetryableJobError(str(exc)) from exc
        except (SnapshotIntegrityError, PermanentSourceError, OSError) as exc:
            finished_at = self._normalize_utc(self._clock())
            self._persist_failure(
                provider_id=provider,
                sport=sport_code,
                acquisition_cycle_id=cycle_id,
                adapter_version=adapter_version,
                observed_at=observed_at,
                started_at=started_at,
                finished_at=finished_at,
                failure_classification="snapshot-publish-error",
                warnings=[warning.code for warning in bundle.warnings],
            )
            raise PermanentJobError(str(exc)) from exc

        _checkpoint()
        finished_at = self._normalize_utc(self._clock())
        published = publication.published
        warning_codes = [warning.code for warning in bundle.warnings]
        self._persist_success(
            provider_id=provider,
            sport=sport_code,
            acquisition_cycle_id=cycle_id,
            adapter_version=adapter_version,
            observed_at=observed_at,
            started_at=started_at,
            finished_at=finished_at,
            snapshot_id=published.snapshot_id,
            relative_path=published.snapshot_relative_path,
            checksum_sha256=published.manifest_checksum_sha256,
            schema_version=published.schema_version,
            events_observed=publication.event_count,
            valid_quotes_observed=publication.quote_count,
            unresolved_events=len(reconciliations.unresolved_event_reconciliations),
            rejected_markets=len(normalized.unknown_markets),
            warnings=warning_codes,
            drift_codes=list(bundle.drift_codes),
            acquisition_interval_seconds=provider_settings.acquisition_interval_seconds,
        )
        return BookmakerIngestionResult(
            provider_id=provider,
            sport=sport_code,
            acquisition_cycle_id=cycle_id,
            adapter_version=adapter_version,
            status="succeeded",
            observed_at_utc=format_utc_timestamp(observed_at),
            snapshot_id=published.snapshot_id,
            snapshot_reused=published.snapshot_reused,
            block_reason=None,
            failure_classification="",
            events_observed=publication.event_count,
            valid_quotes_observed=publication.quote_count,
            unresolved_events=len(reconciliations.unresolved_event_reconciliations),
            rejected_markets=len(normalized.unknown_markets),
            warnings=tuple(warning_codes),
            drift_codes=tuple(bundle.drift_codes),
        )

    def _acquire(
        self,
        *,
        provider_id: str,
        sport: str,
        acquisition_cycle_id: str,
        observed_at_utc: datetime,
        browser_mode: BrowserMode,
    ) -> tuple[BrowserAcquisitionResult, ProviderAcquisitionBundle, tuple[str, ...]]:
        if provider_id == PROVIDER_BETANO_PT:
            return acquire_betano_current_odds(
                sport=sport,
                acquisition_cycle_id=acquisition_cycle_id,
                observed_at_utc=observed_at_utc,
                raw_directory=self._raw_directory,
                browser_mode=browser_mode,
                session=self._session,
            )
        if provider_id == PROVIDER_BETCLIC_PT:
            return acquire_betclic_current_odds(
                sport=sport,
                acquisition_cycle_id=acquisition_cycle_id,
                observed_at_utc=observed_at_utc,
                raw_directory=self._raw_directory,
                browser_mode=browser_mode,
                session=self._session,
            )
        msg = f"unsupported bookmaker provider: {provider_id}"
        raise PermanentJobError(msg)

    def _provider_settings(self, provider_id: str) -> BookmakerProviderSettings:
        if provider_id == PROVIDER_BETANO_PT:
            return self._bookmakers.betano
        if provider_id == PROVIDER_BETCLIC_PT:
            return self._bookmakers.betclic
        msg = f"unsupported bookmaker provider: {provider_id}"
        raise PermanentJobError(msg)

    @staticmethod
    def _warnings_json(warnings: list[str]) -> JsonValue:
        return list(warnings)

    def _browser_mode(self) -> BrowserMode:
        mode = self._bookmakers.browser_mode
        if mode == BrowserMode.VISIBLE.value:
            return BrowserMode.VISIBLE
        if mode == BrowserMode.VISIBLE_MINIMIZED.value:
            return BrowserMode.VISIBLE_MINIMIZED
        msg = f"unsupported browser_mode for acquisition: {mode}"
        raise PermanentJobError(msg)

    @staticmethod
    def _adapter_version(provider_id: str) -> str:
        if provider_id == PROVIDER_BETANO_PT:
            return BETANO_ADAPTER_VERSION
        if provider_id == PROVIDER_BETCLIC_PT:
            return BETCLIC_ADAPTER_VERSION
        msg = f"unsupported bookmaker provider: {provider_id}"
        raise PermanentJobError(msg)

    @staticmethod
    def _normalize_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _persist_blocked(
        self,
        *,
        provider_id: str,
        sport: str,
        acquisition_cycle_id: str,
        adapter_version: str,
        observed_at: datetime,
        started_at: datetime,
        finished_at: datetime,
        block_reason: str,
        failure_classification: str,
        warnings: list[str],
        blocked_cooldown_seconds: int,
    ) -> str:
        next_eligible = finished_at + timedelta(seconds=blocked_cooldown_seconds)
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                repo = BookmakerRepository(connection)
                run_id = repo.insert_acquisition_run(
                    provider_id=provider_id,
                    sport=sport,
                    acquisition_cycle_id=acquisition_cycle_id,
                    adapter_version=adapter_version,
                    status="blocked",
                    observed_at=observed_at,
                    started_at=started_at,
                    finished_at=finished_at,
                    failure_classification=failure_classification,
                    warnings=self._warnings_json(warnings),
                    block_reason=block_reason,
                )
                repo.insert_acquisition_attempt(
                    run_id=run_id,
                    attempt_number=1,
                    started_at=started_at,
                    finished_at=finished_at,
                    outcome="blocked",
                    failure_classification=failure_classification,
                    detail_code=block_reason,
                )
                repo.upsert_provider_status(
                    provider_id=provider_id,
                    status="blocked",
                    updated_at=finished_at,
                    last_attempted_at=finished_at,
                    warnings=self._warnings_json(warnings),
                    block_failure_classification=failure_classification,
                    next_eligible_at=next_eligible,
                    adapter_version=adapter_version,
                    preserve_last_valid_snapshot=True,
                )
                return run_id

    def _persist_failure(
        self,
        *,
        provider_id: str,
        sport: str,
        acquisition_cycle_id: str,
        adapter_version: str,
        observed_at: datetime,
        started_at: datetime,
        finished_at: datetime,
        failure_classification: str,
        warnings: list[str],
    ) -> None:
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                repo = BookmakerRepository(connection)
                run_id = repo.insert_acquisition_run(
                    provider_id=provider_id,
                    sport=sport,
                    acquisition_cycle_id=acquisition_cycle_id,
                    adapter_version=adapter_version,
                    status="failed",
                    observed_at=observed_at,
                    started_at=started_at,
                    finished_at=finished_at,
                    failure_classification=failure_classification,
                    warnings=self._warnings_json(warnings),
                )
                repo.insert_acquisition_attempt(
                    run_id=run_id,
                    attempt_number=1,
                    started_at=started_at,
                    finished_at=finished_at,
                    outcome="failed",
                    failure_classification=failure_classification,
                )
                repo.upsert_provider_status(
                    provider_id=provider_id,
                    status="degraded",
                    updated_at=finished_at,
                    last_attempted_at=finished_at,
                    warnings=self._warnings_json(warnings),
                    block_failure_classification=None,
                    adapter_version=adapter_version,
                    preserve_last_valid_snapshot=True,
                )

    def _persist_success(
        self,
        *,
        provider_id: str,
        sport: str,
        acquisition_cycle_id: str,
        adapter_version: str,
        observed_at: datetime,
        started_at: datetime,
        finished_at: datetime,
        snapshot_id: str,
        relative_path: str,
        checksum_sha256: str,
        schema_version: str,
        events_observed: int,
        valid_quotes_observed: int,
        unresolved_events: int,
        rejected_markets: int,
        warnings: list[str],
        drift_codes: list[str],
        acquisition_interval_seconds: int,
    ) -> None:
        next_eligible = finished_at + timedelta(seconds=acquisition_interval_seconds)
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                repo = BookmakerRepository(connection)
                run_id = repo.insert_acquisition_run(
                    provider_id=provider_id,
                    sport=sport,
                    acquisition_cycle_id=acquisition_cycle_id,
                    adapter_version=adapter_version,
                    status="succeeded",
                    observed_at=observed_at,
                    started_at=started_at,
                    finished_at=finished_at,
                    failure_classification="",
                    warnings=self._warnings_json(warnings),
                    snapshot_id=snapshot_id,
                )
                repo.insert_acquisition_attempt(
                    run_id=run_id,
                    attempt_number=1,
                    started_at=started_at,
                    finished_at=finished_at,
                    outcome="succeeded",
                )
                repo.register_snapshot(
                    snapshot_id=snapshot_id,
                    provider_id=provider_id,
                    sport=sport,
                    schema_version=schema_version,
                    checksum_sha256=checksum_sha256,
                    relative_path=relative_path,
                    observed_at=observed_at,
                    registered_at=finished_at,
                    acquisition_cycle_id=acquisition_cycle_id,
                )
                age = max(0, int((finished_at - observed_at).total_seconds()))
                repo.upsert_provider_status(
                    provider_id=provider_id,
                    status="healthy",
                    updated_at=finished_at,
                    last_attempted_at=finished_at,
                    last_successful_at=finished_at,
                    last_valid_snapshot_id=snapshot_id,
                    snapshot_age_seconds=age,
                    events_observed=events_observed,
                    valid_quotes_observed=valid_quotes_observed,
                    unresolved_events=unresolved_events,
                    rejected_markets=rejected_markets,
                    warnings=self._warnings_json(warnings),
                    block_failure_classification=None,
                    next_eligible_at=next_eligible,
                    adapter_version=adapter_version,
                )
                for code in drift_codes:
                    repo.insert_drift_finding(
                        provider_id=provider_id,
                        run_id=run_id,
                        code=code,
                        severity="warning",
                        message=f"parser drift observed: {code}",
                        observed_at=observed_at,
                    )


def validate_bookmaker_ingest_payload(
    payload: dict[str, object],
) -> tuple[str, str, datetime | None, str | None]:
    """Validate a bookmaker acquisition job payload."""
    allowed = {"provider_id", "sport", "observed_at_utc", "acquisition_cycle_id"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        msg = f"unknown payload keys: {', '.join(unknown)}"
        raise PermanentJobError(msg)
    reject_forbidden_job_controls(payload)
    if "provider_id" not in payload or "sport" not in payload:
        msg = "payload requires provider_id and sport"
        raise PermanentJobError(msg)
    provider_id = payload["provider_id"]
    sport = payload["sport"]
    if not isinstance(provider_id, str) or not isinstance(sport, str):
        msg = "provider_id and sport must be strings"
        raise PermanentJobError(msg)
    observed_raw = payload.get("observed_at_utc")
    observed_at: datetime | None = None
    if observed_raw is not None:
        if not isinstance(observed_raw, str):
            msg = "observed_at_utc must be a string or null"
            raise PermanentJobError(msg)
        try:
            from sports_analytics.data.codec import parse_utc_timestamp

            observed_at = parse_utc_timestamp(observed_raw)
        except ValueError as exc:
            msg = f"invalid observed_at_utc: {observed_raw}"
            raise PermanentJobError(msg) from exc
    cycle_raw = payload.get("acquisition_cycle_id")
    cycle_id: str | None = None
    if cycle_raw is not None:
        if not isinstance(cycle_raw, str):
            msg = "acquisition_cycle_id must be a string or null"
            raise PermanentJobError(msg)
        try:
            cycle_id = validate_identifier(cycle_raw, field_name="acquisition_cycle_id")
        except Exception as exc:  # noqa: BLE001
            raise PermanentJobError(str(exc)) from exc
    return provider_id, sport, observed_at, cycle_id
