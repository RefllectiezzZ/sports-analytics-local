"""Bookmaker current-odds ingestion service."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sports_analytics.bookmakers.admission import (
    AdmissionDecision,
    AdmissionOutcome,
    evaluate_admission,
)
from sports_analytics.bookmakers.normalization import normalize_bookmaker_bundles
from sports_analytics.bookmakers.reconciliation import (
    reconcile_bookmaker_bundles,
    sanitize_competition_events,
)
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
    ProviderStatusCode,
)
from sports_analytics.bookmakers.window import AcquisitionWindow, rolling_acquisition_window
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
from sports_analytics.sources.betano.adapter import acquire_betano_current_odds
from sports_analytics.sources.betano.catalog import ADAPTER_VERSION as BETANO_ADAPTER_VERSION
from sports_analytics.sources.betclic.adapter import acquire_betclic_current_odds
from sports_analytics.sources.betclic.catalog import ADAPTER_VERSION as BETCLIC_ADAPTER_VERSION
from sports_analytics.sources.bookmaker_capture import (
    build_capture_manifest,
    manifest_to_raw_artifact,
    persist_capture_manifest,
    verify_capture_manifest,
)
from sports_analytics.sources.bookmaker_catalog import (
    SUPPORTED_BOOKMAKER_SPORTS,
    reject_forbidden_job_controls,
)
from sports_analytics.sources.bookmaker_contracts import (
    ProviderAcquisitionBundle,
    ProviderEventObservation,
    provider_native_markets,
)
from sports_analytics.sources.browser.contracts import BrowserAcquisitionResult, BrowserMode
from sports_analytics.sources.browser.playwright_runtime import (
    BrowserSession,
    PlaywrightBrowserSession,
)
from sports_analytics.sources.raw_capture import BookmakerRawCapture


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
        self._session = session or PlaywrightBrowserSession(
            navigation_timeout_ms=bookmakers.navigation_timeout_ms,
            maximum_response_bytes=bookmakers.maximum_response_bytes,
            maximum_total_capture_bytes=bookmakers.maximum_total_capture_bytes,
            event_detail_concurrency=bookmakers.event_detail_concurrency,
            minimum_event_detail_interval_ms=(bookmakers.minimum_event_detail_interval_ms),
            explicit_retry_limit=bookmakers.explicit_retry_limit,
        )

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
        attempt_number: int = 1,
        maximum_attempts: int = 2,
        acquisition_window: AcquisitionWindow | None = None,
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
        if attempt_number < 1 or attempt_number > maximum_attempts:
            msg = "attempt_number exceeds maximum_attempts"
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
        window = acquisition_window or rolling_acquisition_window(
            started_at,
            window_hours=self._bookmakers.default_window_hours,
            maximum_events=self._bookmakers.maximum_events_per_sport,
            maximum_window_hours=self._bookmakers.maximum_window_hours,
        )
        if acquisition_window is not None:
            cycle_id = validate_identifier(
                f"{cycle_id}-w{window.identity_digest[:12]}",
                field_name="acquisition_cycle_id",
            )

        existing_run = self._get_existing_run(provider, sport_code, cycle_id)
        if existing_run is not None and existing_run["status"] in {
            "succeeded",
            "blocked",
            "failed",
        }:
            return self._result_from_run(existing_run)

        _checkpoint()
        try:
            browser_result, bundle, captures = self._acquire(
                provider_id=provider,
                sport=sport_code,
                acquisition_cycle_id=cycle_id,
                observed_at_utc=observed_at,
                browser_mode=browser_mode,
                acquisition_window=window,
            )
        except RetryableSourceError as exc:
            finished_at = self._normalize_utc(self._clock())
            self._persist_retryable(
                provider_id=provider,
                sport=sport_code,
                acquisition_cycle_id=cycle_id,
                adapter_version=adapter_version,
                observed_at=observed_at,
                started_at=started_at,
                finished_at=finished_at,
                attempt_number=attempt_number,
                failure_classification="retryable-source-error",
                detail_code="retryable-source-error",
                warnings=[],
            )
            if attempt_number >= maximum_attempts:
                self._finalize_terminal_failure(
                    provider_id=provider,
                    sport=sport_code,
                    acquisition_cycle_id=cycle_id,
                    adapter_version=adapter_version,
                    observed_at=observed_at,
                    finished_at=finished_at,
                    failure_classification="retry-exhausted",
                    warnings=[],
                )
                raise PermanentJobError("retry attempts exhausted") from exc
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
                attempt_number=attempt_number,
                failure_classification="permanent-source-error",
                warnings=[],
            )
            raise PermanentJobError(str(exc)) from exc

        response_observation_count = len(browser_result.responses)
        recognized_profile_response_count = bundle.recognized_profile_response_count
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
                attempt_number=attempt_number,
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
                response_observation_count=response_observation_count,
                recognized_profile_response_count=recognized_profile_response_count,
            )

        _checkpoint()
        try:
            from sports_analytics.bookmakers.normalization import EMPTY_SOURCE_FILE_SHA256

            evidence_sha256 = next(
                (
                    capture.checksum_sha256
                    for capture in captures
                    if capture.capture_kind == "provider-json"
                ),
                EMPTY_SOURCE_FILE_SHA256,
            )
            if isinstance(bundle, ProviderAcquisitionBundle):
                bundle = sanitize_competition_events((bundle,))[0]
            reconciliations = reconcile_bookmaker_bundles((bundle,))
            normalized = normalize_bookmaker_bundles(
                (bundle,),
                reconciliations=reconciliations,
                source_file_sha256=evidence_sha256,
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
                attempt_number=attempt_number,
                failure_classification="normalize-error",
                warnings=[warning.code for warning in bundle.warnings],
            )
            raise PermanentJobError(str(exc)) from exc

        native_recognized = _verified_extraction_applied(bundle)
        admission = evaluate_admission(
            browser_result=browser_result,
            bundle=bundle,
            normalized=normalized,
            valid_quote_count=len(normalized.market_quotes),
            unresolved_event_count=len(reconciliations.unresolved_event_reconciliations),
            verified_extraction_applied=native_recognized,
        )
        if not admission.may_publish:
            finished_at = self._normalize_utc(self._clock())
            status = _ingestion_status_for_admission(admission)
            self._persist_rejected(
                provider_id=provider,
                sport=sport_code,
                acquisition_cycle_id=cycle_id,
                adapter_version=adapter_version,
                observed_at=observed_at,
                started_at=started_at,
                finished_at=finished_at,
                attempt_number=attempt_number,
                admission=admission,
                provider_status=_provider_status_for_admission(admission),
                warnings=list(admission.warnings),
                drift_codes=list(bundle.drift_codes),
                blocked_cooldown_seconds=provider_settings.blocked_cooldown_seconds,
            )
            return BookmakerIngestionResult(
                provider_id=provider,
                sport=sport_code,
                acquisition_cycle_id=cycle_id,
                adapter_version=adapter_version,
                status=status,
                observed_at_utc=format_utc_timestamp(observed_at),
                snapshot_id=None,
                snapshot_reused=False,
                block_reason=admission.reason_code if status == "blocked" else None,
                failure_classification=admission.reason_code,
                events_observed=len(bundle.events),
                valid_quotes_observed=len(normalized.market_quotes),
                unresolved_events=len(reconciliations.unresolved_event_reconciliations),
                rejected_markets=len(normalized.unknown_markets),
                warnings=admission.warnings,
                drift_codes=tuple(bundle.drift_codes),
                response_observation_count=response_observation_count,
                recognized_profile_response_count=recognized_profile_response_count,
            )

        _checkpoint()
        manifest = build_capture_manifest(
            provider_id=provider,
            acquisition_cycle_id=cycle_id,
            captures=captures,
        )
        manifest = persist_capture_manifest(raw_directory=self._raw_directory, manifest=manifest)
        verify_capture_manifest(raw_directory=self._raw_directory, manifest=manifest)
        raw_artifact = manifest_to_raw_artifact(manifest)
        source_version = build_bookmaker_source_version(
            sport_code=sport_code,
            acquisition_cycle_id=cycle_id,
            raw_sha256=manifest.checksum_sha256,
        )
        status_record = build_provider_status(
            provider_id=provider,
            adapter_version=adapter_version,
            observed_at_utc=observed_at,
            last_attempted_acquisition_utc=started_at,
            last_successful_acquisition_utc=started_at,
            last_valid_snapshot_id=None,
            snapshot_age_seconds=0,
            events_observed=len(bundle.events),
            valid_quotes_observed=len(normalized.market_quotes),
            unresolved_events=len(reconciliations.unresolved_event_reconciliations),
            rejected_markets=len(normalized.unknown_markets),
            warnings=tuple(sorted({warning.code for warning in bundle.warnings})),
            current_block_or_failure_classification=FailureClassification.NONE,
            next_eligible_attempt_utc=None,
            drift_detected=bool(bundle.drift_codes),
            acquisition_partial=not admission.exhaustive_capture_complete,
            provider_native_markets=_native_market_count(bundle),
            provider_native_priced_selections=_native_selection_count(bundle),
            canonical_markets=_canonical_market_count(normalized),
            canonical_quotes=len(normalized.market_quotes),
            unmapped_markets=len(normalized.unknown_markets),
            non_comparable_quotes=sum(
                1 for item in normalized.comparison_eligibility if not item.comparable
            ),
            complete_events=_complete_event_count(bundle),
            partial_events=len(bundle.events) - _complete_event_count(bundle),
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
                domain_metadata={
                    "capture_manifest_relative_path": manifest.relative_path,
                    "capture_manifest_checksum_sha256": manifest.checksum_sha256,
                },
                provider_bundle=bundle,
            )
        except SnapshotBusyError as exc:
            finished_at = self._normalize_utc(self._clock())
            self._persist_retryable(
                provider_id=provider,
                sport=sport_code,
                acquisition_cycle_id=cycle_id,
                adapter_version=adapter_version,
                observed_at=observed_at,
                started_at=started_at,
                finished_at=finished_at,
                attempt_number=attempt_number,
                failure_classification="snapshot-busy",
                detail_code="snapshot-busy",
                warnings=[warning.code for warning in bundle.warnings],
            )
            if attempt_number >= maximum_attempts:
                self._finalize_terminal_failure(
                    provider_id=provider,
                    sport=sport_code,
                    acquisition_cycle_id=cycle_id,
                    adapter_version=adapter_version,
                    observed_at=observed_at,
                    finished_at=finished_at,
                    failure_classification="retry-exhausted",
                    warnings=[warning.code for warning in bundle.warnings],
                )
                raise PermanentJobError("retry attempts exhausted") from exc
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
                attempt_number=attempt_number,
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
            attempt_number=attempt_number,
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
            provider_status_code=(
                ProviderStatusCode.OPERATIONAL.value
                if admission.exhaustive_capture_complete
                else ProviderStatusCode.PARTIAL.value
            ),
            replace_last_valid=admission.may_replace_last_valid,
        )
        return BookmakerIngestionResult(
            provider_id=provider,
            sport=sport_code,
            acquisition_cycle_id=cycle_id,
            adapter_version=adapter_version,
            status=("succeeded" if admission.exhaustive_capture_complete else "partial"),
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
            response_observation_count=response_observation_count,
            recognized_profile_response_count=recognized_profile_response_count,
        )

    def _acquire(
        self,
        *,
        provider_id: str,
        sport: str,
        acquisition_cycle_id: str,
        observed_at_utc: datetime,
        browser_mode: BrowserMode,
        acquisition_window: AcquisitionWindow,
    ) -> tuple[
        BrowserAcquisitionResult,
        ProviderAcquisitionBundle,
        tuple[BookmakerRawCapture, ...],
    ]:
        if provider_id == PROVIDER_BETANO_PT:
            return acquire_betano_current_odds(
                sport=sport,
                acquisition_cycle_id=acquisition_cycle_id,
                observed_at_utc=observed_at_utc,
                raw_directory=self._raw_directory,
                browser_mode=browser_mode,
                session=self._session,
                acquisition_window=acquisition_window,
            )
        if provider_id == PROVIDER_BETCLIC_PT:
            return acquire_betclic_current_odds(
                sport=sport,
                acquisition_cycle_id=acquisition_cycle_id,
                observed_at_utc=observed_at_utc,
                raw_directory=self._raw_directory,
                browser_mode=browser_mode,
                session=self._session,
                acquisition_window=acquisition_window,
            )
        msg = f"unsupported bookmaker provider: {provider_id}"
        raise PermanentJobError(msg)

    def _get_existing_run(
        self,
        provider_id: str,
        sport: str,
        cycle_id: str,
    ) -> dict[str, JsonValue] | None:
        with connect_database(self._database_path, read_only=True) as connection:
            return BookmakerRepository(connection).get_acquisition_run(
                provider_id=provider_id,
                sport=sport,
                acquisition_cycle_id=cycle_id,
            )

    def _result_from_run(self, run: dict[str, JsonValue]) -> BookmakerIngestionResult:
        warnings = run.get("warnings")
        if isinstance(warnings, list):
            warning_tuple: tuple[str, ...] = tuple(str(item) for item in warnings)
        else:
            warning_tuple = ()
        return BookmakerIngestionResult(
            provider_id=str(run["provider_id"]),
            sport=str(run["sport"]),
            acquisition_cycle_id=str(run["acquisition_cycle_id"]),
            adapter_version=str(run["adapter_version"]),
            status=str(run["status"]),
            observed_at_utc=str(run["observed_at_utc"]),
            snapshot_id=(None if run.get("snapshot_id") is None else str(run["snapshot_id"])),
            snapshot_reused=False,
            block_reason=(None if run.get("block_reason") is None else str(run["block_reason"])),
            failure_classification=str(run["failure_classification"]),
            events_observed=0,
            valid_quotes_observed=0,
            unresolved_events=0,
            rejected_markets=0,
            warnings=warning_tuple,
            drift_codes=(),
        )

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
        if mode == BrowserMode.HEADLESS.value:
            return BrowserMode.HEADLESS
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

    def _ensure_run(
        self,
        *,
        repo: BookmakerRepository,
        provider_id: str,
        sport: str,
        acquisition_cycle_id: str,
        adapter_version: str,
        observed_at: datetime,
        started_at: datetime,
        status: str,
    ) -> str:
        existing = repo.get_acquisition_run(
            provider_id=provider_id,
            sport=sport,
            acquisition_cycle_id=acquisition_cycle_id,
        )
        if existing is not None:
            return str(existing["id"])
        return repo.insert_acquisition_run(
            provider_id=provider_id,
            sport=sport,
            acquisition_cycle_id=acquisition_cycle_id,
            adapter_version=adapter_version,
            status=status,
            observed_at=observed_at,
            started_at=started_at,
            finished_at=started_at,
            failure_classification="",
            warnings=self._warnings_json([]),
        )

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
        attempt_number: int,
        block_reason: str,
        failure_classification: str,
        warnings: list[str],
        blocked_cooldown_seconds: int,
    ) -> str:
        next_eligible = finished_at + timedelta(seconds=blocked_cooldown_seconds)
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                repo = BookmakerRepository(connection)
                run_id = self._ensure_run(
                    repo=repo,
                    provider_id=provider_id,
                    sport=sport,
                    acquisition_cycle_id=acquisition_cycle_id,
                    adapter_version=adapter_version,
                    observed_at=observed_at,
                    started_at=started_at,
                    status="blocked",
                )
                repo.insert_acquisition_attempt(
                    run_id=run_id,
                    attempt_number=attempt_number,
                    started_at=started_at,
                    finished_at=finished_at,
                    outcome="blocked",
                    failure_classification=failure_classification,
                    detail_code=block_reason,
                )
                repo.update_acquisition_run_status(
                    run_id=run_id,
                    status="blocked",
                    finished_at=finished_at,
                    failure_classification=failure_classification,
                    block_reason=block_reason,
                )
                repo.upsert_provider_status(
                    provider_id=provider_id,
                    sport=sport,
                    status=ProviderStatusCode.BLOCKED.value,
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
        attempt_number: int,
        failure_classification: str,
        warnings: list[str],
    ) -> None:
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                repo = BookmakerRepository(connection)
                run_id = self._ensure_run(
                    repo=repo,
                    provider_id=provider_id,
                    sport=sport,
                    acquisition_cycle_id=acquisition_cycle_id,
                    adapter_version=adapter_version,
                    observed_at=observed_at,
                    started_at=started_at,
                    status="failed",
                )
                repo.insert_acquisition_attempt(
                    run_id=run_id,
                    attempt_number=attempt_number,
                    started_at=started_at,
                    finished_at=finished_at,
                    outcome="failed",
                    failure_classification=failure_classification,
                )
                repo.update_acquisition_run_status(
                    run_id=run_id,
                    status="failed",
                    finished_at=finished_at,
                    failure_classification=failure_classification,
                )
                repo.upsert_provider_status(
                    provider_id=provider_id,
                    sport=sport,
                    status=ProviderStatusCode.UNAVAILABLE.value,
                    updated_at=finished_at,
                    last_attempted_at=finished_at,
                    warnings=self._warnings_json(warnings),
                    block_failure_classification=None,
                    adapter_version=adapter_version,
                    preserve_last_valid_snapshot=True,
                )

    def _finalize_terminal_failure(
        self,
        *,
        provider_id: str,
        sport: str,
        acquisition_cycle_id: str,
        adapter_version: str,
        observed_at: datetime,
        finished_at: datetime,
        failure_classification: str,
        warnings: list[str],
    ) -> None:
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                repo = BookmakerRepository(connection)
                existing = repo.get_acquisition_run(
                    provider_id=provider_id,
                    sport=sport,
                    acquisition_cycle_id=acquisition_cycle_id,
                )
                if existing is None:
                    return
                repo.update_acquisition_run_status(
                    run_id=str(existing["id"]),
                    status="failed",
                    finished_at=finished_at,
                    failure_classification=failure_classification,
                )
                repo.upsert_provider_status(
                    provider_id=provider_id,
                    sport=sport,
                    status=ProviderStatusCode.UNAVAILABLE.value,
                    updated_at=finished_at,
                    last_attempted_at=finished_at,
                    warnings=self._warnings_json(warnings),
                    adapter_version=adapter_version,
                    preserve_last_valid_snapshot=True,
                )

    def _persist_retryable(
        self,
        *,
        provider_id: str,
        sport: str,
        acquisition_cycle_id: str,
        adapter_version: str,
        observed_at: datetime,
        started_at: datetime,
        finished_at: datetime,
        attempt_number: int,
        failure_classification: str,
        detail_code: str,
        warnings: list[str],
    ) -> None:
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                repo = BookmakerRepository(connection)
                run_id = self._ensure_run(
                    repo=repo,
                    provider_id=provider_id,
                    sport=sport,
                    acquisition_cycle_id=acquisition_cycle_id,
                    adapter_version=adapter_version,
                    observed_at=observed_at,
                    started_at=started_at,
                    status="retryable",
                )
                repo.insert_acquisition_attempt(
                    run_id=run_id,
                    attempt_number=attempt_number,
                    started_at=started_at,
                    finished_at=finished_at,
                    outcome="retryable",
                    failure_classification=failure_classification,
                    detail_code=detail_code,
                )
                repo.update_acquisition_run_status(
                    run_id=run_id,
                    status="retryable",
                    finished_at=finished_at,
                    failure_classification=failure_classification,
                )
                repo.upsert_provider_status(
                    provider_id=provider_id,
                    sport=sport,
                    status=ProviderStatusCode.UNAVAILABLE.value,
                    updated_at=finished_at,
                    last_attempted_at=finished_at,
                    warnings=self._warnings_json(warnings),
                    adapter_version=adapter_version,
                    preserve_last_valid_snapshot=True,
                )

    def _persist_rejected(
        self,
        *,
        provider_id: str,
        sport: str,
        acquisition_cycle_id: str,
        adapter_version: str,
        observed_at: datetime,
        started_at: datetime,
        finished_at: datetime,
        attempt_number: int,
        admission: AdmissionDecision,
        provider_status: str,
        warnings: list[str],
        drift_codes: list[str],
        blocked_cooldown_seconds: int,
    ) -> None:
        run_status = _run_status_for_admission(admission)
        next_eligible = (
            finished_at + timedelta(seconds=blocked_cooldown_seconds)
            if admission.outcome is AdmissionOutcome.BLOCKED
            else None
        )
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                repo = BookmakerRepository(connection)
                run_id = self._ensure_run(
                    repo=repo,
                    provider_id=provider_id,
                    sport=sport,
                    acquisition_cycle_id=acquisition_cycle_id,
                    adapter_version=adapter_version,
                    observed_at=observed_at,
                    started_at=started_at,
                    status=run_status,
                )
                repo.insert_acquisition_attempt(
                    run_id=run_id,
                    attempt_number=attempt_number,
                    started_at=started_at,
                    finished_at=finished_at,
                    outcome=run_status if run_status != "retryable" else "failed",
                    failure_classification=admission.reason_code,
                    detail_code=admission.reason_code,
                )
                repo.update_acquisition_run_status(
                    run_id=run_id,
                    status=run_status,
                    finished_at=finished_at,
                    failure_classification=admission.reason_code,
                    block_reason=(
                        admission.reason_code
                        if admission.outcome is AdmissionOutcome.BLOCKED
                        else None
                    ),
                )
                repo.upsert_provider_status(
                    provider_id=provider_id,
                    sport=sport,
                    status=provider_status,
                    updated_at=finished_at,
                    last_attempted_at=finished_at,
                    warnings=self._warnings_json(warnings),
                    block_failure_classification=(
                        admission.reason_code
                        if admission.outcome is AdmissionOutcome.BLOCKED
                        else None
                    ),
                    next_eligible_at=next_eligible,
                    adapter_version=adapter_version,
                    preserve_last_valid_snapshot=True,
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
        attempt_number: int,
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
        provider_status_code: str,
        replace_last_valid: bool,
    ) -> None:
        next_eligible = finished_at + timedelta(seconds=acquisition_interval_seconds)
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                repo = BookmakerRepository(connection)
                run_id = self._ensure_run(
                    repo=repo,
                    provider_id=provider_id,
                    sport=sport,
                    acquisition_cycle_id=acquisition_cycle_id,
                    adapter_version=adapter_version,
                    observed_at=observed_at,
                    started_at=started_at,
                    status="succeeded",
                )
                repo.insert_acquisition_attempt(
                    run_id=run_id,
                    attempt_number=attempt_number,
                    started_at=started_at,
                    finished_at=finished_at,
                    outcome="succeeded",
                )
                repo.update_acquisition_run_status(
                    run_id=run_id,
                    status="succeeded",
                    finished_at=finished_at,
                    failure_classification="",
                    snapshot_id=snapshot_id,
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
                    sport=sport,
                    status=provider_status_code,
                    updated_at=finished_at,
                    last_attempted_at=finished_at,
                    last_successful_at=finished_at,
                    last_valid_snapshot_id=(snapshot_id if replace_last_valid else None),
                    snapshot_age_seconds=age,
                    events_observed=events_observed,
                    valid_quotes_observed=valid_quotes_observed,
                    unresolved_events=unresolved_events,
                    rejected_markets=rejected_markets,
                    warnings=self._warnings_json(warnings),
                    block_failure_classification=None,
                    next_eligible_at=next_eligible,
                    adapter_version=adapter_version,
                    preserve_last_valid_snapshot=not replace_last_valid,
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


def _verified_extraction_applied(bundle: ProviderAcquisitionBundle) -> bool:
    rejected = frozenset(
        {
            "unverified-extraction-profile",
            "no-verified-extraction-profile",
            "no-adapter-contract",
            "unknown-schema",
        }
    )
    if any(code in rejected for code in bundle.drift_codes):
        return False
    return len(bundle.events) > 0


def _native_market_count(bundle: ProviderAcquisitionBundle) -> int:
    return sum(
        len(provider_native_markets(event))
        if isinstance(event, ProviderEventObservation)
        else len(getattr(event, "markets", ()))
        for event in bundle.events
    )


def _native_selection_count(bundle: ProviderAcquisitionBundle) -> int:
    return sum(
        len(getattr(market, "selections", ()))
        for event in bundle.events
        for market in (
            provider_native_markets(event)
            if isinstance(event, ProviderEventObservation)
            else getattr(event, "markets", ())
        )
    )


def _canonical_market_count(normalized: object) -> int:
    identities: set[str] = set()
    quotes = getattr(normalized, "market_quotes", ())
    for quote in quotes:
        try:
            identities.add(str(quote.selection.definition.market_key))
        except AttributeError:
            identities.add(str(id(quote)))
    return len(identities)


def _complete_event_count(bundle: ProviderAcquisitionBundle) -> int:
    count = 0
    for event in bundle.events:
        completeness = getattr(event, "completeness", None)
        state = getattr(getattr(completeness, "completeness_state", None), "value", "")
        if str(state).startswith("complete-"):
            count += 1
    return count


def _ingestion_status_for_admission(admission: AdmissionDecision) -> str:
    if admission.outcome is AdmissionOutcome.BLOCKED:
        return "blocked"
    if admission.outcome is AdmissionOutcome.DRIFT_DETECTED:
        return "drift-detected"
    if admission.outcome is AdmissionOutcome.PARTIAL:
        return "partial"
    if admission.outcome is AdmissionOutcome.UNAVAILABLE:
        return "unavailable"
    return "failed"


def _run_status_for_admission(admission: AdmissionDecision) -> str:
    if admission.outcome is AdmissionOutcome.BLOCKED:
        return "blocked"
    return "failed"


def _provider_status_for_admission(admission: AdmissionDecision) -> str:
    mapping = {
        AdmissionOutcome.BLOCKED: ProviderStatusCode.BLOCKED.value,
        AdmissionOutcome.DRIFT_DETECTED: ProviderStatusCode.DRIFT_DETECTED.value,
        AdmissionOutcome.PARTIAL: ProviderStatusCode.PARTIAL.value,
        AdmissionOutcome.UNAVAILABLE: ProviderStatusCode.UNAVAILABLE.value,
        AdmissionOutcome.FAILED: ProviderStatusCode.UNAVAILABLE.value,
    }
    return mapping.get(admission.outcome, ProviderStatusCode.UNKNOWN.value)


def validate_bookmaker_ingest_payload(
    payload: dict[str, object],
) -> tuple[str, str, datetime | None, str | None, AcquisitionWindow]:
    """Validate a bookmaker acquisition job payload."""
    allowed = {
        "provider_id",
        "sport",
        "observed_at_utc",
        "acquisition_cycle_id",
        "acquisition_window",
    }
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
    if "acquisition_window" not in payload:
        msg = "payload requires acquisition_window"
        raise PermanentJobError(msg)
    window = AcquisitionWindow.from_payload(payload["acquisition_window"])
    return provider_id, sport, observed_at, cycle_id, window
