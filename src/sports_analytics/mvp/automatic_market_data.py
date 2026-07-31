"""Automatic The Odds API setup, durable scheduling, acquisition, and risk ranking."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Final, cast

from sports_analytics.artifacts import (
    AnalyticalArtifact,
    load_analytical_artifact,
    write_analytical_artifact,
)
from sports_analytics.core.exceptions import (
    ArtifactError,
    ConfigurationError,
    PermanentJobError,
    PermanentSourceError,
    RetryableSourceError,
    SportsAnalyticsError,
)
from sports_analytics.core.paths import RuntimePaths, resolve_paths
from sports_analytics.core.runtime import RuntimeContext
from sports_analytics.core.settings import Settings, load_settings
from sports_analytics.data.codec import dumps_canonical_json, format_utc_timestamp
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.job_queue import JobQueueRepository
from sports_analytics.data.repositories.jobs import JobRepository
from sports_analytics.data.types import JobRecord, JobStatus, JsonValue
from sports_analytics.jobs.context import JobExecutionContext
from sports_analytics.providers.the_odds_api.client import (
    ALLOWED_MARKETS,
    ALLOWED_REGIONS,
    THE_ODDS_API_HOST,
    THE_ODDS_API_PROVIDER_ID,
    ApiHttpTransport,
    ProviderSecret,
    TheOddsApiAuthenticationError,
    TheOddsApiClient,
    TheOddsApiQuotaError,
    TheOddsApiRetryableError,
)
from sports_analytics.providers.the_odds_api.contracts import (
    ProviderOddsBatch,
    ProviderQuota,
)
from sports_analytics.providers.the_odds_api.mapping import (
    COMPETITION_TO_SPORT_KEY,
    ReconciledProviderEvent,
    UnresolvedProviderEvent,
    provider_sport_key,
    reconcile_provider_event,
    translate_bookmaker_quotes,
)
from sports_analytics.services.production_football_product import (
    PublishedProductionFootballProduct,
)
from sports_analytics.sports.football.participant_registry import FootballParticipantRegistry
from sports_analytics.upcoming_events import (
    UpcomingEvent,
    load_upcoming_event_artifact,
    write_upcoming_event_artifact,
)

AUTOMATIC_MARKET_DATA_JOB_TYPE: Final[str] = "acquire.the-odds-api-market-data"
AUTOMATIC_CONFIG_SCHEMA: Final[str] = "automatic-market-data-config-v1"
AUTOMATIC_STATE_SCHEMA: Final[str] = "automatic-market-data-state-v1"
AUTOMATIC_SECRET_SCHEMA: Final[str] = "the-odds-api-secret-v1"
RAW_PROVIDER_ARTIFACT_TYPE: Final[str] = "the-odds-api-raw-response"
RAW_PROVIDER_ARTIFACT_SCHEMA: Final[str] = "the-odds-api-raw-response-v1"
RISK_ARTIFACT_TYPE: Final[str] = "risk-adjusted-opportunities"
RISK_ARTIFACT_SCHEMA: Final[str] = "risk-adjusted-opportunities-v1"
DEFAULT_INTERVAL_MINUTES: Final[int] = 10
MINIMUM_INTERVAL_MINUTES: Final[int] = 5
MAXIMUM_INTERVAL_MINUTES: Final[int] = 60
DEFAULT_QUOTA_RESERVE: Final[int] = 20
DEFAULT_WINDOW_HOURS: Final[int] = 168
MAXIMUM_ATTEMPTS: Final[int] = 3
RANKING_POLICY: Final[tuple[str, ...]] = (
    "placeable-manual-before-analytical-before-held-before-rejected",
    "lower-risk-tier",
    "higher-valid-expected-value",
    "higher-edge",
    "fresher-quote",
    "broader-bookmaker-coverage",
    "canonical-identity",
)
_CONFIG_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "enabled",
        "paused",
        "authentication_blocked",
        "region",
        "competitions",
        "markets",
        "refresh_interval_minutes",
        "quota_reserve",
        "generation",
        "updated_at_utc",
    }
)


@dataclass(frozen=True, slots=True)
class AutomaticProviderConfig:
    """Non-secret locally persisted provider configuration."""

    enabled: bool
    paused: bool
    authentication_blocked: bool
    region: str
    competitions: tuple[str, ...]
    markets: tuple[str, ...]
    refresh_interval_minutes: int
    quota_reserve: int
    generation: int
    updated_at_utc: datetime

    def __post_init__(self) -> None:
        if self.region not in ALLOWED_REGIONS:
            raise ConfigurationError("automatic provider region is not allowlisted")
        if (
            not self.competitions
            or any(item not in COMPETITION_TO_SPORT_KEY for item in self.competitions)
            or tuple(sorted(set(self.competitions))) != self.competitions
        ):
            raise ConfigurationError("automatic competitions must use explicit mappings")
        if (
            not self.markets
            or any(item not in ALLOWED_MARKETS for item in self.markets)
            or tuple(sorted(set(self.markets))) != self.markets
            or "h2h" not in self.markets
        ):
            raise ConfigurationError("automatic markets must include allowlisted h2h")
        if not (
            MINIMUM_INTERVAL_MINUTES <= self.refresh_interval_minutes <= MAXIMUM_INTERVAL_MINUTES
        ):
            raise ConfigurationError("automatic refresh interval must be 5 through 60 minutes")
        if type(self.quota_reserve) is not int or self.quota_reserve < 0:
            raise ConfigurationError("automatic quota reserve must be non-negative")
        if type(self.generation) is not int or self.generation < 1:
            raise ConfigurationError("automatic configuration generation is invalid")
        if self.updated_at_utc.tzinfo is None:
            raise ConfigurationError("automatic configuration timestamp must be timezone-aware")

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "schema_version": AUTOMATIC_CONFIG_SCHEMA,
            "enabled": self.enabled,
            "paused": self.paused,
            "authentication_blocked": self.authentication_blocked,
            "region": self.region,
            "competitions": list(self.competitions),
            "markets": list(self.markets),
            "refresh_interval_minutes": self.refresh_interval_minutes,
            "quota_reserve": self.quota_reserve,
            "generation": self.generation,
            "updated_at_utc": format_utc_timestamp(self.updated_at_utc),
        }


@dataclass(frozen=True, slots=True)
class AutomaticOperationStatus:
    """UI-safe automatic operation state. It can never contain a secret."""

    configured: bool
    enabled: bool
    paused: bool
    authentication_blocked: bool
    provider_connection_state: str
    region: str | None
    competitions: tuple[str, ...]
    markets: tuple[str, ...]
    refresh_interval_minutes: int | None
    last_successful_acquisition: str | None
    next_scheduled_acquisition: str | None
    quota_remaining: int | None
    quota_used: int | None
    quota_last_cost: int | None
    last_warning: str | None
    last_failure: str | None
    events_discovered: int
    events_reconciled: int
    unresolved_events: tuple[dict[str, str], ...]
    bookmakers_observed: int
    valid_current_quote_count: int
    last_known_good_product_at: str | None
    ranked_opportunities: tuple[dict[str, JsonValue], ...]
    risk_artifact_relative_directory: str | None
    risk_artifact_id: str | None


class AutomaticProviderStore:
    """Narrow atomic local store, separate from analytical artifacts."""

    def __init__(self, storage_root: Path) -> None:
        self.directory = storage_root / "local" / "automatic-market-data"
        self.config_path = self.directory / "config.json"
        self.secret_path = self.directory / "the-odds-api.secret.json"
        self.state_path = self.directory / "state.json"
        self.sports_cache_path = self.directory / "sports-catalogue.json"

    def load_config(self) -> AutomaticProviderConfig | None:
        value = self._read_json(self.config_path, absent_ok=True)
        if value is None:
            return None
        if set(value) != _CONFIG_FIELDS or value.get("schema_version") != AUTOMATIC_CONFIG_SCHEMA:
            raise ConfigurationError("automatic provider configuration is malformed")
        return AutomaticProviderConfig(
            enabled=_bool(value["enabled"], "enabled"),
            paused=_bool(value["paused"], "paused"),
            authentication_blocked=_bool(
                value["authentication_blocked"],
                "authentication_blocked",
            ),
            region=_text(value["region"], "region"),
            competitions=_sorted_text_tuple(value["competitions"], "competitions"),
            markets=_sorted_text_tuple(value["markets"], "markets"),
            refresh_interval_minutes=_int(value["refresh_interval_minutes"], "interval"),
            quota_reserve=_int(value["quota_reserve"], "quota_reserve"),
            generation=_int(value["generation"], "generation"),
            updated_at_utc=_timestamp(value["updated_at_utc"], "updated_at_utc"),
        )

    def save_config(self, config: AutomaticProviderConfig) -> None:
        self._write_json(self.config_path, config.to_json(), secret=False)

    def load_secret(self) -> ProviderSecret | None:
        value = self._read_json(self.secret_path, absent_ok=True)
        if value is None:
            return None
        if set(value) != {"schema_version", "api_key"}:
            raise ConfigurationError("automatic provider secret file is malformed")
        if value.get("schema_version") != AUTOMATIC_SECRET_SCHEMA:
            raise ConfigurationError("automatic provider secret schema is unsupported")
        return ProviderSecret(_text(value["api_key"], "api_key"))

    def save_secret(self, secret: ProviderSecret) -> None:
        self._write_json(
            self.secret_path,
            {
                "schema_version": AUTOMATIC_SECRET_SCHEMA,
                "api_key": secret.api_key,
            },
            secret=True,
        )

    def load_state(self) -> dict[str, JsonValue]:
        value = self._read_json(self.state_path, absent_ok=True)
        if value is None:
            return _empty_state()
        if value.get("schema_version") != AUTOMATIC_STATE_SCHEMA:
            raise ConfigurationError("automatic provider state schema is unsupported")
        return cast(dict[str, JsonValue], value)

    def save_state(self, state: Mapping[str, JsonValue]) -> None:
        payload = dict(state)
        payload["schema_version"] = AUTOMATIC_STATE_SCHEMA
        self._write_json(self.state_path, payload, secret=False)

    def save_sports_cache(self, payload: Mapping[str, JsonValue]) -> None:
        self._write_json(self.sports_cache_path, dict(payload), secret=False)

    def _read_json(self, path: Path, *, absent_ok: bool) -> dict[str, object] | None:
        if not path.is_file():
            if absent_ok:
                return None
            raise ConfigurationError("automatic provider local file is absent")
        if path.is_symlink() or path.stat().st_size > 4_194_304:
            raise ConfigurationError("automatic provider local file is unsafe")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError("automatic provider local file is malformed") from exc
        if not isinstance(value, dict) or any(type(key) is not str for key in value):
            raise ConfigurationError("automatic provider local file must be an object")
        return cast(dict[str, object], value)

    def _write_json(
        self,
        path: Path,
        payload: Mapping[str, JsonValue],
        *,
        secret: bool,
    ) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=self.directory,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(dumps_canonical_json(dict(payload)) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if secret:
                os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temporary, path)
            if secret:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


class AutomaticMarketDataController:
    """Allowlisted setup and operational controls used by the MVP UI."""

    def __init__(
        self,
        *,
        base_directory: Path,
        config_path: Path | str | None = None,
        env_file: Path | str | None = None,
        clock: Callable[[], datetime] | None = None,
        transport: ApiHttpTransport | None = None,
    ) -> None:
        self.base_directory = base_directory
        self.config_path = config_path
        self.env_file = env_file
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._transport = transport

    def inspect(self) -> AutomaticOperationStatus:
        settings, paths = self._settings_paths()
        del settings
        store = AutomaticProviderStore(paths.storage_root)
        config = store.load_config()
        state = store.load_state()
        return _operation_status(config, state, secret_present=store.load_secret() is not None)

    def enable(
        self,
        *,
        api_key: str,
        region: str,
        competitions: tuple[str, ...],
        markets: tuple[str, ...],
        refresh_interval_minutes: int,
        quota_reserve: int = DEFAULT_QUOTA_RESERVE,
    ) -> AutomaticOperationStatus:
        """Validate, prepare, persist, and enqueue one immediate acquisition."""
        from sports_analytics.mvp.orchestrator import MVPOrchestrator

        now = self._now()
        settings, paths = self._settings_paths()
        candidate = AutomaticProviderConfig(
            enabled=True,
            paused=False,
            authentication_blocked=False,
            region=region,
            competitions=tuple(sorted(set(competitions))),
            markets=tuple(sorted(set(markets))),
            refresh_interval_minutes=refresh_interval_minutes,
            quota_reserve=quota_reserve,
            generation=1,
            updated_at_utc=now,
        )
        secret = ProviderSecret(api_key)
        client = TheOddsApiClient(
            secret=secret,
            transport=self._transport,
            clock=lambda: now,
        )
        catalogue = client.get_sports()
        available = {item.key for item in catalogue.sports if item.active}
        missing = [
            provider_sport_key(competition)
            for competition in candidate.competitions
            if provider_sport_key(competition) not in available
        ]
        if missing:
            raise ConfigurationError(
                "selected competitions are unavailable in the provider sports catalogue"
            )
        orchestrator = MVPOrchestrator(
            base_directory=self.base_directory,
            config_path=self.config_path,
            env_file=self.env_file,
            clock=lambda: now,
        )
        preparation = orchestrator.prepare_system()
        unsupported = sorted(set(candidate.competitions) - set(preparation.active_competitions))
        if unsupported:
            raise ConfigurationError(
                "automatic competitions require a governance-authorized active champion"
            )
        store = AutomaticProviderStore(paths.storage_root)
        prior = store.load_config()
        if prior is not None:
            candidate = replace(candidate, generation=prior.generation + 1)
        store.save_secret(secret)
        store.save_sports_cache(
            {
                "schema_version": "the-odds-api-sports-cache-v1",
                "acquired_at_utc": format_utc_timestamp(catalogue.acquired_at_utc),
                "expires_at_utc": format_utc_timestamp(
                    catalogue.acquired_at_utc + timedelta(hours=24)
                ),
                "content_sha256": catalogue.content_sha256,
                "quota": _quota_json(catalogue.quota),
                "sports": [
                    {
                        "key": item.key,
                        "group": item.group,
                        "title": item.title,
                        "active": item.active,
                    }
                    for item in catalogue.sports
                ],
            }
        )
        store.save_config(candidate)
        state = _empty_state()
        state.update(
            {
                "provider_connection_state": "validated",
                "quota_remaining": catalogue.quota.remaining,
                "quota_used": catalogue.quota.used,
                "quota_last_cost": catalogue.quota.last_cost,
                "last_warning": None,
                "last_failure": None,
            }
        )
        store.save_state(state)
        ensure_automatic_market_data_job(
            database_path=paths.sqlite_path,
            config=candidate,
            due_at_utc=now,
            actor="automatic-setup",
        )
        return _operation_status(candidate, state, secret_present=True)

    def replace_key(self, api_key: str) -> AutomaticOperationStatus:
        settings, paths = self._settings_paths()
        del settings
        store = AutomaticProviderStore(paths.storage_root)
        config = store.load_config()
        if config is None:
            raise ConfigurationError("automatic operation is not configured")
        now = self._now()
        secret = ProviderSecret(api_key)
        catalogue = TheOddsApiClient(
            secret=secret,
            transport=self._transport,
            clock=lambda: now,
        ).get_sports()
        updated = replace(
            config,
            enabled=True,
            paused=False,
            authentication_blocked=False,
            generation=config.generation + 1,
            updated_at_utc=now,
        )
        store.save_secret(secret)
        store.save_config(updated)
        state = store.load_state()
        state.update(
            {
                "provider_connection_state": "validated",
                "quota_remaining": catalogue.quota.remaining,
                "quota_used": catalogue.quota.used,
                "quota_last_cost": catalogue.quota.last_cost,
                "last_failure": None,
                "last_warning": None,
            }
        )
        store.save_state(state)
        ensure_automatic_market_data_job(
            database_path=paths.sqlite_path,
            config=updated,
            due_at_utc=now,
            actor="automatic-key-replacement",
            replace_pending=True,
        )
        return _operation_status(updated, state, secret_present=True)

    def pause(self) -> AutomaticOperationStatus:
        return self._set_paused(True)

    def resume(self) -> AutomaticOperationStatus:
        status = self._set_paused(False)
        settings, paths = self._settings_paths()
        del settings
        config = AutomaticProviderStore(paths.storage_root).load_config()
        if config is not None:
            ensure_automatic_market_data_job(
                database_path=paths.sqlite_path,
                config=config,
                due_at_utc=self._now(),
                actor="automatic-resume",
                replace_pending=True,
            )
        return status

    def run_now(self) -> str:
        settings, paths = self._settings_paths()
        del settings
        config = AutomaticProviderStore(paths.storage_root).load_config()
        if config is None or not config.enabled or config.paused or config.authentication_blocked:
            raise ConfigurationError("automatic acquisition is not currently enabled")
        job = ensure_automatic_market_data_job(
            database_path=paths.sqlite_path,
            config=config,
            due_at_utc=self._now(),
            actor="automatic-run-now",
            replace_pending=True,
        )
        return job.id

    def _set_paused(self, paused: bool) -> AutomaticOperationStatus:
        settings, paths = self._settings_paths()
        del settings
        store = AutomaticProviderStore(paths.storage_root)
        config = store.load_config()
        if config is None:
            raise ConfigurationError("automatic operation is not configured")
        updated = replace(
            config,
            paused=paused,
            generation=config.generation + 1,
            updated_at_utc=self._now(),
        )
        store.save_config(updated)
        if paused:
            _cancel_pending_automatic_jobs(
                database_path=paths.sqlite_path,
                actor="automatic-pause",
                cancelled_at=self._now(),
            )
        return _operation_status(updated, store.load_state(), secret_present=True)

    def _settings_paths(self) -> tuple[Settings, RuntimePaths]:
        settings = load_settings(
            base_directory=self.base_directory,
            config_path=self.config_path,
            env_file=self.env_file,
        )
        return settings, resolve_paths(settings, self.base_directory)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ConfigurationError("automatic operation clock must be timezone-aware")
        return value.astimezone(UTC)


def ensure_automatic_market_data_job(
    *,
    database_path: Path,
    config: AutomaticProviderConfig,
    due_at_utc: datetime,
    actor: str,
    replace_pending: bool = False,
    exclude_job_id: str | None = None,
    retry_number: int = 0,
) -> JobRecord:
    """Atomically ensure one equivalent pending/running automatic job."""
    due = due_at_utc.astimezone(UTC)
    with connect_database(database_path) as connection:
        with transaction(connection, immediate=True):
            jobs = JobRepository(connection)
            existing = [
                item
                for status in (JobStatus.PENDING, JobStatus.RUNNING)
                for item in jobs.list_jobs(
                    status=status,
                    job_type=AUTOMATIC_MARKET_DATA_JOB_TYPE,
                    limit=1_000,
                )
                if item.id != exclude_job_id
            ]
            if replace_pending:
                queue = JobQueueRepository(connection)
                for item in existing:
                    if item.status is JobStatus.PENDING:
                        queue.cancel_pending_job(
                            job_id=item.id,
                            expected_status=item.status,
                            expected_version=item.version,
                            cancelled_at=due,
                            actor=actor,
                            details={"reason": "replaced-by-immediate-automatic-job"},
                        )
                existing = [item for item in existing if item.status is JobStatus.RUNNING]
            if existing:
                return sorted(existing, key=lambda item: (item.created_at, item.id))[0]
            due_text = format_utc_timestamp(due)
            return jobs.create_job(
                job_type=AUTOMATIC_MARKET_DATA_JOB_TYPE,
                payload={
                    "generation": config.generation,
                    "retry_number": retry_number,
                },
                maximum_attempts=MAXIMUM_ATTEMPTS,
                actor=actor,
                available_at=due,
                created_at=min(self_now := datetime.now(tz=UTC), due),
                idempotency_key=(
                    f"automatic-market-data:{config.generation}:{due_text}:"
                    f"{int(self_now.timestamp() * 1_000_000)}"
                ),
            )


def ensure_startup_automatic_job(
    *,
    paths: RuntimePaths,
    now: datetime,
) -> str | None:
    """Resume one enabled automatic operation during normal startup."""
    store = AutomaticProviderStore(paths.storage_root)
    config = store.load_config()
    if (
        config is None
        or not config.enabled
        or config.paused
        or config.authentication_blocked
        or store.load_secret() is None
    ):
        return None
    job = ensure_automatic_market_data_job(
        database_path=paths.sqlite_path,
        config=config,
        due_at_utc=now,
        actor="automatic-startup",
    )
    return job.id


def automatic_market_data_handler(
    context: JobExecutionContext,
    payload: JsonValue,
) -> JsonValue:
    """Durable automatic acquisition handler with bounded queue retries."""
    if not isinstance(payload, dict) or set(payload) != {"generation", "retry_number"}:
        raise PermanentJobError("automatic market-data job payload fields are not exact")
    generation = payload.get("generation")
    retry_number = payload.get("retry_number")
    if type(generation) is not int or generation < 1:
        raise PermanentJobError("automatic market-data job generation is invalid")
    if type(retry_number) is not int or not 0 <= retry_number <= 2:
        raise PermanentJobError("automatic market-data retry number is invalid")
    runtime = getattr(context, "_runtime_context", None)
    if runtime is None:
        raise PermanentJobError("automatic market-data handler requires runtime binding")
    store = AutomaticProviderStore(runtime.paths.storage_root)
    config = store.load_config()
    if config is None or not config.enabled or config.paused:
        return {"state": "disabled-or-paused", "network_requested": False}
    if config.authentication_blocked:
        return {"state": "authentication-blocked", "network_requested": False}
    if config.generation != generation:
        ensure_automatic_market_data_job(
            database_path=runtime.paths.sqlite_path,
            config=config,
            due_at_utc=datetime.now(tz=UTC),
            actor="automatic-generation-refresh",
            exclude_job_id=context.job_id,
        )
        return {"state": "superseded", "network_requested": False}
    secret = store.load_secret()
    if secret is None:
        _record_failure(store, "provider API key is absent", connection_state="not-configured")
        return {"state": "secret-absent", "network_requested": False}
    try:
        result = run_automatic_acquisition(
            runtime=runtime,
            config=config,
            secret=secret,
            transport=context._http_transport,
            clock=context._clock,
        )
    except TheOddsApiAuthenticationError:
        blocked = replace(
            config,
            authentication_blocked=True,
            generation=config.generation + 1,
            updated_at_utc=datetime.now(tz=UTC),
        )
        store.save_config(blocked)
        _record_failure(
            store,
            "The Odds API authentication failed; replace the API key to resume.",
            connection_state="authentication-failed",
        )
        return {"state": "authentication-failed", "network_requested": True}
    except TheOddsApiQuotaError as exc:
        _record_failure(store, str(exc), connection_state="quota-reserve-paused", warning=True)
        return {"state": "quota-reserve-paused", "network_requested": False}
    except RetryableSourceError as exc:
        if retry_number < 2:
            delay = _retry_delay_with_jitter(
                job_id=context.job_id,
                retry_number=retry_number,
            )
            if isinstance(exc, TheOddsApiRetryableError):
                delay = max(delay, exc.retry_after_seconds or 0)
            retry_due = datetime.now(tz=UTC) + timedelta(seconds=delay)
            ensure_automatic_market_data_job(
                database_path=runtime.paths.sqlite_path,
                config=config,
                due_at_utc=retry_due,
                actor="automatic-bounded-retry",
                exclude_job_id=context.job_id,
                retry_number=retry_number + 1,
            )
            state = store.load_state()
            state["provider_connection_state"] = "retry-scheduled"
            state["last_warning"] = (
                "Temporary provider failure; bounded retry scheduled while "
                "last-known-good product remains available."
            )
            state["next_scheduled_acquisition"] = format_utc_timestamp(retry_due)
            store.save_state(state)
            return {
                "state": "retry-scheduled",
                "retry_number": retry_number + 1,
                "retry_delay_seconds": delay,
                "last_known_good_retained": True,
            }
        _record_failure(
            store,
            "Temporary provider failure; last-known-good product retained.",
            connection_state="temporarily-unavailable",
            warning=True,
        )
        _schedule_next(runtime.paths, config, context.job_id)
        return {"state": "temporarily-unavailable", "last_known_good_retained": True}
    except (PermanentSourceError, ConfigurationError, ArtifactError, SportsAnalyticsError) as exc:
        _record_failure(
            store,
            _safe_error(exc),
            connection_state="validation-failed",
            warning=True,
        )
        _schedule_next(runtime.paths, config, context.job_id)
        return {"state": "validation-failed", "last_known_good_retained": True}
    _schedule_next(runtime.paths, config, context.job_id)
    return cast(JsonValue, result)


def run_automatic_acquisition(
    *,
    runtime: RuntimeContext,
    config: AutomaticProviderConfig,
    secret: ProviderSecret,
    transport: object | None = None,
    clock: object | None = None,
) -> dict[str, JsonValue]:
    """Acquire, reconcile, publish, analyse, and rank one bounded cycle."""
    from sports_analytics.mvp.orchestrator import MVPOrchestrator

    paths = runtime.paths
    settings = runtime.settings
    now = (
        cast(Callable[[], datetime], clock)() if callable(clock) else datetime.now(tz=UTC)
    ).astimezone(UTC)
    store = AutomaticProviderStore(paths.storage_root)
    state = store.load_state()
    known_remaining = _optional_int(state.get("quota_remaining"))
    client = TheOddsApiClient(
        secret=secret,
        transport=cast(ApiHttpTransport | None, transport),
        clock=lambda: now,
    )
    orchestrator = MVPOrchestrator(base_directory=paths.base_directory, clock=lambda: now)
    registry = orchestrator._require_registry(paths)
    discovered = 0
    reconciled_count = 0
    unresolved: list[UnresolvedProviderEvent] = []
    bookmakers: set[str] = set()
    valid_quote_count = 0
    changed = False
    products: list[str] = []
    ranked: tuple[dict[str, JsonValue], ...] = ()
    risk_artifact: AnalyticalArtifact | None = None
    digests = _string_mapping(state.get("content_sha256_by_sport"))
    latest_quota = None
    for competition in config.competitions:
        if orchestrator._champion_for(paths, competition) is None:
            unresolved.append(UnresolvedProviderEvent("", "", "", "active-champion-unavailable"))
            continue
        sport_key = provider_sport_key(competition)
        batch = client.get_odds(
            sport_key=sport_key,
            regions=(config.region,),
            markets=config.markets,
            commence_time_from=now,
            commence_time_to=now + timedelta(hours=DEFAULT_WINDOW_HOURS),
            quota_reserve=config.quota_reserve,
            known_remaining=known_remaining,
        )
        latest_quota = batch.quota
        known_remaining = batch.quota.remaining
        discovered += len(batch.events)
        bookmakers.update(item.key for event in batch.events for item in event.bookmakers)
        if digests.get(sport_key) == batch.content_sha256:
            continue
        _publish_raw_batch(paths=paths, batch=batch)
        reconciled_events: list[ReconciledProviderEvent] = []
        for event in batch.events:
            reconciled, finding = reconcile_provider_event(
                event,
                registry=registry,
                acquired_at_utc=now,
            )
            if finding is not None:
                unresolved.append(finding)
            elif reconciled is not None:
                reconciled_events.append(reconciled)
        reconciled_count += len(reconciled_events)
        if not reconciled_events:
            digests[sport_key] = batch.content_sha256
            changed = True
            continue
        canonical_events = tuple(item.canonical for item in reconciled_events)
        event_artifact = _publish_events(
            paths=paths,
            registry=registry,
            events=canonical_events,
            evaluated_at_utc=now,
            content_sha256=batch.content_sha256,
        )
        quotes = tuple(
            quote
            for item in reconciled_events
            for quote in translate_bookmaker_quotes(
                item,
                acquired_at_utc=now,
                enabled_markets=config.markets,
                freshness=timedelta(
                    seconds=max(settings.bookmakers.quote_maximum_age_seconds, 1_800)
                ),
            )
        )
        fresh_quotes = tuple(
            item
            for item in quotes
            if item.observed_at_utc <= now
            and now - item.observed_at_utc
            <= timedelta(seconds=settings.bookmakers.quote_maximum_age_seconds)
            and (item.valid_until_utc is None or item.valid_until_utc >= now)
        )
        valid_quote_count += len(fresh_quotes)
        if fresh_quotes:
            published = orchestrator.run_automatic_analysis(
                settings=settings,
                paths=paths,
                registry=registry,
                event_artifact=event_artifact,
                events=canonical_events,
                provider_quotes=fresh_quotes,
                evaluated_at_utc=now,
            )
            products.append(published.read_model_artifact.artifact_id)
            ranked = _rank_opportunities(
                published=published,
                registry=registry,
                events=canonical_events,
                evaluated_at_utc=now,
            )
            risk_artifact = _publish_risk_artifact(
                paths=paths,
                product=published.read_model_artifact,
                ranked=ranked,
                evaluated_at_utc=now,
            )
        digests[sport_key] = batch.content_sha256
        changed = True
    state.update(
        {
            "provider_connection_state": "connected",
            "last_successful_acquisition": format_utc_timestamp(now),
            "next_scheduled_acquisition": format_utc_timestamp(
                now + timedelta(minutes=config.refresh_interval_minutes)
            ),
            "quota_remaining": (
                state.get("quota_remaining") if latest_quota is None else latest_quota.remaining
            ),
            "quota_used": state.get("quota_used") if latest_quota is None else latest_quota.used,
            "quota_last_cost": (
                state.get("quota_last_cost") if latest_quota is None else latest_quota.last_cost
            ),
            "last_warning": (
                "Automatic acquisition succeeded, but no fresh complete quote was available."
                if changed and not valid_quote_count
                else None
            ),
            "last_failure": None,
            "events_discovered": discovered,
            "events_reconciled": reconciled_count,
            "unresolved_events": [
                {
                    "provider_event_id": item.provider_event_id,
                    "home_team": item.home_team,
                    "away_team": item.away_team,
                    "reason": item.reason,
                }
                for item in sorted(
                    unresolved,
                    key=lambda item: (
                        item.provider_event_id,
                        item.home_team,
                        item.away_team,
                        item.reason,
                    ),
                )
            ],
            "bookmakers_observed": len(bookmakers),
            "valid_current_quote_count": valid_quote_count,
            "content_sha256_by_sport": dict(sorted(digests.items())),
            "last_known_good_product_at": (
                format_utc_timestamp(now) if products else state.get("last_known_good_product_at")
            ),
            "last_product_artifact_ids": cast(
                JsonValue,
                products or state.get("last_product_artifact_ids", []),
            ),
            "ranked_opportunities": list(ranked)
            if ranked
            else state.get("ranked_opportunities", []),
            "risk_artifact_relative_directory": (
                risk_artifact.relative_directory
                if risk_artifact is not None
                else state.get("risk_artifact_relative_directory")
            ),
            "risk_artifact_id": (
                risk_artifact.artifact_id
                if risk_artifact is not None
                else state.get("risk_artifact_id")
            ),
            "ranking_policy": list(RANKING_POLICY),
        }
    )
    store.save_state(state)
    return {
        "state": "succeeded",
        "changed": changed,
        "events_discovered": discovered,
        "events_reconciled": reconciled_count,
        "unresolved_event_count": len(unresolved),
        "bookmakers_observed": len(bookmakers),
        "valid_quote_count": valid_quote_count,
        "product_artifact_ids": cast(JsonValue, products),
        "last_known_good_retained": True,
    }


def _publish_raw_batch(
    *,
    paths: RuntimePaths,
    batch: ProviderOddsBatch,
) -> AnalyticalArtifact:
    relative = f"the-odds-api/raw/{batch.sport_key}/{batch.content_sha256}"
    payload: dict[str, JsonValue] = {
        "provider_id": THE_ODDS_API_PROVIDER_ID,
        "provider_host": THE_ODDS_API_HOST,
        "sport_key": batch.sport_key,
        "content_sha256": batch.content_sha256,
        "quota": _quota_json(batch.quota),
        "response": cast(JsonValue, batch.canonical_payload),
    }
    return _publish_or_reuse(
        root=paths.snapshots_directory,
        relative=relative,
        artifact_type=RAW_PROVIDER_ARTIFACT_TYPE,
        schema=RAW_PROVIDER_ARTIFACT_SCHEMA,
        payload=payload,
    )


def _publish_events(
    *,
    paths: RuntimePaths,
    registry: FootballParticipantRegistry,
    events: tuple[UpcomingEvent, ...],
    evaluated_at_utc: datetime,
    content_sha256: str,
) -> AnalyticalArtifact:
    identity = _digest(
        {
            "content_sha256": content_sha256,
            "events": [item.to_json() for item in events],
        }
    )
    relative = f"mvp/automatic-upcoming-events/{events[0].competition_id}/{identity}"
    try:
        return write_upcoming_event_artifact(
            root=paths.exports_directory,
            relative_directory=relative,
            events=events,
            evaluated_at_utc=evaluated_at_utc,
            participant_registry=registry,
        )
    except ArtifactError:
        artifact, loaded = load_upcoming_event_artifact(
            root=paths.exports_directory,
            relative_directory=relative,
        )
        if loaded != tuple(sorted(events, key=lambda item: item.canonical_event_id)):
            raise ArtifactError("automatic upcoming-event replay conflicts") from None
        return artifact


def _rank_opportunities(
    *,
    published: PublishedProductionFootballProduct,
    registry: FootballParticipantRegistry,
    events: tuple[UpcomingEvent, ...],
    evaluated_at_utc: datetime,
) -> tuple[dict[str, JsonValue], ...]:
    proposals = published.proposals
    catalogue = published.quote_catalogue
    if proposals is None:
        return ()
    event_index = {item.canonical_event_id: item for item in events}
    provider_names = (
        {}
        if catalogue is None
        else {item.input.provider_id: item.input.provider_display_name for item in catalogue.quotes}
    )
    rows: list[dict[str, JsonValue]] = []
    for decision in proposals.decisions:
        event = event_index.get(decision.canonical_event_id)
        if event is None:
            continue
        home = registry.participant(event.canonical_home_participant_id)
        away = registry.participant(event.canonical_away_participant_id)
        comparable = (
            ()
            if catalogue is None
            else tuple(
                item
                for item in catalogue.quotes
                if item.input.canonical_event_id == decision.canonical_event_id
                and item.input.market_family == decision.market.market_family
                and item.input.outcome_key == decision.market.outcome_key
                and item.input.line_value == decision.market.line_value
                and item.input.market_period == decision.market.market_period
                and item.market_complete
            )
        )
        prices = sorted(item.offered_decimal_odds for item in comparable)
        best_quote = max(
            comparable,
            key=lambda item: (
                item.offered_decimal_odds,
                tuple(-ord(character) for character in item.input.provider_id),
            ),
            default=None,
        )
        best_price = None if best_quote is None else best_quote.offered_decimal_odds
        median_price = None if not prices else Decimal(str(median(prices)))
        dispersion = (
            None
            if len(prices) < 2 or median_price in {None, Decimal("0")}
            else (max(prices) - min(prices)) / median_price
        )
        age_seconds = (
            None
            if best_quote is None
            else max(0.0, (evaluated_at_utc - best_quote.input.observed_at_utc).total_seconds())
        )
        overround = None
        if best_quote is not None and catalogue is not None:
            complete_market = tuple(
                item
                for item in catalogue.quotes
                if item.input.canonical_event_id == decision.canonical_event_id
                and item.input.provider_id == best_quote.input.provider_id
                and item.input.market_family == decision.market.market_family
                and item.input.line_value == decision.market.line_value
                and item.input.market_period == decision.market.market_period
                and item.market_complete
            )
            if complete_market:
                overround = sum(1.0 / float(item.offered_decimal_odds) for item in complete_market)
        status = _proposal_status(
            accepted=decision.accepted,
            offered=decision.offered_decimal_odds,
            reasons=decision.reason_codes,
        )
        risk, risk_reasons = _risk_tier(
            status=status,
            coverage=len(comparable),
            age_seconds=age_seconds,
            dispersion=dispersion,
            complete=bool(comparable),
            uncertainty=decision.uncertainty_state,
        )
        identity = decision.decision_id
        reasons = tuple(sorted(set((*decision.reason_codes, *risk_reasons))))
        explanation = (
            "Complete fresh real-price evidence with calibrated model output."
            if risk == "low"
            else "; ".join(reason.replace("-", " ") for reason in reasons)
            or "Evidence requires manual review."
        )
        rows.append(
            {
                "canonical_identity": identity,
                "event": (
                    f"{home.canonical_display_name if home else 'Unresolved home'} v "
                    f"{away.canonical_display_name if away else 'Unresolved away'}"
                ),
                "canonical_event_id": decision.canonical_event_id,
                "competition": event.competition_id,
                "kickoff_time_utc": format_utc_timestamp(event.event_start_utc),
                "selection": decision.market.outcome_key,
                "market": decision.market.market_family,
                "line": (
                    None
                    if decision.market.line_value is None
                    else format(decision.market.line_value, "f")
                ),
                "best_bookmaker": (
                    None
                    if best_quote is None
                    else provider_names.get(
                        best_quote.input.provider_id,
                        best_quote.input.provider_display_name,
                    )
                ),
                "best_bookmaker_id": (None if best_quote is None else best_quote.input.provider_id),
                "best_offered_price": (None if best_price is None else format(best_price, "f")),
                "median_available_price": (
                    None if median_price is None else format(median_price, "f")
                ),
                "price_dispersion": (None if dispersion is None else float(dispersion)),
                "market_overround": overround,
                "price_comparison": [
                    {
                        "bookmaker": item.input.provider_display_name,
                        "bookmaker_id": item.input.provider_id,
                        "offered_price": format(item.offered_decimal_odds, "f"),
                        "observed_at_utc": format_utc_timestamp(item.input.observed_at_utc),
                    }
                    for item in sorted(
                        comparable,
                        key=lambda item: (
                            -item.offered_decimal_odds,
                            item.input.provider_id,
                        ),
                    )
                ],
                "model_probability": decision.model_probability,
                "fair_odds": decision.fair_decimal_odds,
                "edge": decision.edge,
                "expected_value": decision.expected_value,
                "risk_tier": risk,
                "risk_reason_codes": list(reasons),
                "risk_explanation": explanation,
                "observed_price_age_seconds": age_seconds,
                "bookmaker_coverage_count": len(comparable),
                "complete_market": bool(comparable),
                "model_calibration_available": (
                    "model-calibration-failed" not in decision.reason_codes
                ),
                "model_evidence_eligible": status in {"placeable", "analytical"},
                "dependency_status": "single-selection",
                "status": status,
                "hold_reason": (
                    None if not decision.reason_codes else ", ".join(decision.reason_codes)
                ),
                "placement_state": decision.placement_state,
            }
        )
    return tuple(sorted(rows, key=_ranking_key))


def _publish_risk_artifact(
    *,
    paths: RuntimePaths,
    product: AnalyticalArtifact,
    ranked: tuple[dict[str, JsonValue], ...],
    evaluated_at_utc: datetime,
) -> AnalyticalArtifact:
    payload: dict[str, JsonValue] = {
        "product_artifact_id": product.artifact_id,
        "product_checksum_sha256": product.checksum_sha256,
        "evaluated_at_utc": format_utc_timestamp(evaluated_at_utc),
        "ranking_policy": list(RANKING_POLICY),
        "top_label": "Top risk-adjusted opportunity",
        "opportunities": list(ranked),
    }
    identity = _digest(payload)
    return _publish_or_reuse(
        root=paths.exports_directory,
        relative=f"mvp/automatic-risk/{identity}",
        artifact_type=RISK_ARTIFACT_TYPE,
        schema=RISK_ARTIFACT_SCHEMA,
        payload=payload,
    )


def _publish_or_reuse(
    *,
    root: Path,
    relative: str,
    artifact_type: str,
    schema: str,
    payload: dict[str, JsonValue],
) -> AnalyticalArtifact:
    try:
        return write_analytical_artifact(
            root=root,
            relative_directory=relative,
            artifact_type=artifact_type,
            schema_version=schema,
            payload=payload,
        )
    except ArtifactError:
        existing = load_analytical_artifact(
            root=root,
            relative_directory=relative,
            expected_artifact_type=artifact_type,
            expected_schema_version=schema,
        )
        if existing.payload != payload:
            raise ArtifactError("automatic artifact replay conflicts") from None
        return existing


def _schedule_next(paths: RuntimePaths, config: AutomaticProviderConfig, job_id: str) -> None:
    next_due = datetime.now(tz=UTC) + timedelta(minutes=config.refresh_interval_minutes)
    ensure_automatic_market_data_job(
        database_path=paths.sqlite_path,
        config=config,
        due_at_utc=next_due,
        actor="automatic-interval",
        exclude_job_id=job_id,
    )
    store = AutomaticProviderStore(paths.storage_root)
    state = store.load_state()
    state["next_scheduled_acquisition"] = format_utc_timestamp(next_due)
    store.save_state(state)


def _cancel_pending_automatic_jobs(
    *,
    database_path: Path,
    actor: str,
    cancelled_at: datetime,
) -> None:
    with connect_database(database_path) as connection:
        with transaction(connection, immediate=True):
            jobs = JobRepository(connection)
            queue = JobQueueRepository(connection)
            for item in jobs.list_jobs(
                status=JobStatus.PENDING,
                job_type=AUTOMATIC_MARKET_DATA_JOB_TYPE,
                limit=1_000,
            ):
                queue.cancel_pending_job(
                    job_id=item.id,
                    expected_status=item.status,
                    expected_version=item.version,
                    cancelled_at=cancelled_at,
                    actor=actor,
                    details={"reason": "automatic-operation-paused"},
                )


def _record_failure(
    store: AutomaticProviderStore,
    message: str,
    *,
    connection_state: str,
    warning: bool = False,
) -> None:
    state = store.load_state()
    state["provider_connection_state"] = connection_state
    state["last_warning" if warning else "last_failure"] = message
    state["last_failure" if warning else "last_warning"] = None
    store.save_state(state)


def _operation_status(
    config: AutomaticProviderConfig | None,
    state: Mapping[str, JsonValue],
    *,
    secret_present: bool,
) -> AutomaticOperationStatus:
    rows = state.get("ranked_opportunities")
    unresolved = state.get("unresolved_events")
    return AutomaticOperationStatus(
        configured=config is not None and secret_present,
        enabled=bool(config and config.enabled),
        paused=bool(config and config.paused),
        authentication_blocked=bool(config and config.authentication_blocked),
        provider_connection_state=str(state.get("provider_connection_state", "not-configured")),
        region=None if config is None else config.region,
        competitions=() if config is None else config.competitions,
        markets=() if config is None else config.markets,
        refresh_interval_minutes=None if config is None else config.refresh_interval_minutes,
        last_successful_acquisition=_optional_text(state.get("last_successful_acquisition")),
        next_scheduled_acquisition=_optional_text(state.get("next_scheduled_acquisition")),
        quota_remaining=_optional_int(state.get("quota_remaining")),
        quota_used=_optional_int(state.get("quota_used")),
        quota_last_cost=_optional_int(state.get("quota_last_cost")),
        last_warning=_optional_text(state.get("last_warning")),
        last_failure=_optional_text(state.get("last_failure")),
        events_discovered=_non_negative_int(state.get("events_discovered")),
        events_reconciled=_non_negative_int(state.get("events_reconciled")),
        unresolved_events=tuple(
            cast(dict[str, str], item) for item in unresolved if isinstance(item, dict)
        )
        if isinstance(unresolved, list)
        else (),
        bookmakers_observed=_non_negative_int(state.get("bookmakers_observed")),
        valid_current_quote_count=_non_negative_int(state.get("valid_current_quote_count")),
        last_known_good_product_at=_optional_text(state.get("last_known_good_product_at")),
        ranked_opportunities=tuple(item for item in rows if isinstance(item, dict))
        if isinstance(rows, list)
        else (),
        risk_artifact_relative_directory=_optional_text(
            state.get("risk_artifact_relative_directory")
        ),
        risk_artifact_id=_optional_text(state.get("risk_artifact_id")),
    )


def _empty_state() -> dict[str, JsonValue]:
    return {
        "schema_version": AUTOMATIC_STATE_SCHEMA,
        "provider_connection_state": "not-configured",
        "last_successful_acquisition": None,
        "next_scheduled_acquisition": None,
        "quota_remaining": None,
        "quota_used": None,
        "quota_last_cost": None,
        "last_warning": None,
        "last_failure": None,
        "events_discovered": 0,
        "events_reconciled": 0,
        "unresolved_events": [],
        "bookmakers_observed": 0,
        "valid_current_quote_count": 0,
        "content_sha256_by_sport": {},
        "last_known_good_product_at": None,
        "last_product_artifact_ids": [],
        "ranked_opportunities": [],
        "risk_artifact_relative_directory": None,
        "risk_artifact_id": None,
        "ranking_policy": list(RANKING_POLICY),
    }


def _proposal_status(
    *,
    accepted: bool,
    offered: Decimal | None,
    reasons: tuple[str, ...],
) -> str:
    if accepted:
        return "placeable"
    if offered is None:
        return "rejected"
    analytical_reasons = {
        "edge-insufficient",
        "conservative-ev-insufficient",
        "offered-odds-below-minimum",
        "offered-odds-above-maximum",
    }
    if set(reasons).issubset(analytical_reasons):
        return "analytical"
    return "held"


def _risk_tier(
    *,
    status: str,
    coverage: int,
    age_seconds: float | None,
    dispersion: Decimal | None,
    complete: bool,
    uncertainty: str,
) -> tuple[str, tuple[str, ...]]:
    reasons: list[str] = []
    if status == "rejected" or not complete:
        reasons.append("insufficient-complete-price-evidence")
        return "insufficient-evidence", tuple(reasons)
    if status == "held":
        reasons.append("proposal-held")
    if age_seconds is None or age_seconds > 300:
        reasons.append("quote-stale-or-age-unavailable")
    if uncertainty != "reviewed":
        reasons.append("model-uncertainty-elevated")
    if status == "held" or any("stale" in item for item in reasons):
        return "high", tuple(reasons)
    if coverage < 2:
        reasons.append("single-bookmaker-coverage")
    if dispersion is not None and dispersion > Decimal("0.10"):
        reasons.append("wide-price-dispersion")
    if reasons:
        return "moderate", tuple(reasons)
    return "low", ()


def _ranking_key(row: dict[str, JsonValue]) -> tuple[object, ...]:
    status = {"placeable": 0, "analytical": 1, "held": 2, "rejected": 3}
    risk = {"low": 0, "moderate": 1, "high": 2, "insufficient-evidence": 3}
    ev = row.get("expected_value")
    edge = row.get("edge")
    age = row.get("observed_price_age_seconds")
    coverage = row.get("bookmaker_coverage_count")
    return (
        status.get(str(row.get("status")), 4),
        risk.get(str(row.get("risk_tier")), 4),
        -float(ev) if isinstance(ev, int | float) and not isinstance(ev, bool) else float("inf"),
        -float(edge)
        if isinstance(edge, int | float) and not isinstance(edge, bool)
        else float("inf"),
        float(age) if isinstance(age, int | float) and not isinstance(age, bool) else float("inf"),
        -int(coverage) if type(coverage) is int else 0,
        str(row.get("canonical_identity", "")),
    )


def _retry_delay_with_jitter(*, job_id: str, retry_number: int) -> int:
    """Return bounded exponential delay plus deterministic per-job jitter."""
    base: int = 5 * (2 ** int(retry_number))
    digest = hashlib.sha256(f"{job_id}:{retry_number}".encode()).digest()
    jitter: int = int.from_bytes(digest[:2], "big") % max(1, base // 2 + 1)
    return int(min(60, base + jitter))


def _quota_json(quota: ProviderQuota) -> dict[str, JsonValue]:
    return {
        "remaining": quota.remaining,
        "used": quota.used,
        "last_cost": quota.last_cost,
    }


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _safe_error(exc: BaseException) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return text[:500] or type(exc).__name__


def _bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ConfigurationError(f"automatic {field} must be boolean")
    return value


def _int(value: object, field: str) -> int:
    if type(value) is not int:
        raise ConfigurationError(f"automatic {field} must be an integer")
    return value


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ConfigurationError(f"automatic {field} must be non-empty trimmed text")
    return value


def _sorted_text_tuple(value: object, field: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or any(type(item) is not str or not item for item in value)
        or cast(list[str], value) != sorted(set(cast(list[str], value)))
    ):
        raise ConfigurationError(f"automatic {field} must be a sorted unique array")
    return tuple(cast(list[str], value))


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    if not text.endswith("Z"):
        raise ConfigurationError(f"automatic {field} must be canonical UTC")
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00").astimezone(UTC)
    except ValueError as exc:
        raise ConfigurationError(f"automatic {field} is malformed") from exc


def _optional_text(value: object) -> str | None:
    return value if type(value) is str and value else None


def _optional_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _non_negative_int(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): str(item) for key, item in value.items() if type(key) is str and type(item) is str
    }
