"""Narrow operational MVP coordinator over existing strict services."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, cast

from sports_analytics.artifacts import (
    ANALYTICAL_MANIFEST_FILENAME,
    AnalyticalArtifact,
    load_analytical_artifact,
)
from sports_analytics.bookmakers.operator_quotes import (
    OPERATOR_QUOTE_ARTIFACT_SCHEMA,
    OPERATOR_QUOTE_ARTIFACT_TYPE,
    OperatorQuoteInput,
    OperatorQuotePolicy,
    operator_quote_identity_payload,
)
from sports_analytics.core.exceptions import (
    ArtifactError,
    ConfigurationError,
    SportsAnalyticsError,
)
from sports_analytics.core.paths import RuntimePaths, resolve_paths
from sports_analytics.core.settings import Settings, load_settings
from sports_analytics.data.database import connect_database
from sports_analytics.data.migrations import get_migration_status
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.governance.contracts import ModelLifecycleStatus, ModelRole
from sports_analytics.governance.repository import ModelGovernanceRepository
from sports_analytics.ingestion.football import enqueue_football_data_ingestion
from sports_analytics.jobs.errors import sanitize_error_text
from sports_analytics.jobs.service import WorkerService
from sports_analytics.mvp.champion_preparation import prepare_score_champions
from sports_analytics.mvp.operator_inputs import (
    MatchOption,
    MatchValidation,
    OddsValidation,
    build_match_options,
    publish_human_matches,
    validate_human_matches,
    validate_human_odds,
)
from sports_analytics.mvp.state import (
    MVPReadinessFacts,
    MVPState,
    MVPStatus,
    determine_mvp_state,
    setup_steps,
)
from sports_analytics.policies.proposal import (
    PublishedProposalPolicy,
    load_published_proposal_policy,
    publish_proposal_policy,
)
from sports_analytics.release.cli import initialize_v1
from sports_analytics.services.champion_resolution import resolve_active_score_champion
from sports_analytics.services.football_product import (
    FOOTBALL_PRODUCT_READ_MODEL_SCHEMA,
    FOOTBALL_PRODUCT_READ_MODEL_TYPE,
)
from sports_analytics.services.production_football_product import (
    ProductionFootballProductRequest,
    PublishedProductionFootballProduct,
    run_and_publish_production_football_product,
)
from sports_analytics.snapshots.reader import verify_snapshot_directory
from sports_analytics.sources.types import SOURCE_FOOTBALL_DATA_CO_UK
from sports_analytics.sports.football.contracts import (
    FOOTBALL_CANONICAL_SCHEMA_VERSION,
    FOOTBALL_INGESTION_SNAPSHOT_TYPE,
)
from sports_analytics.sports.football.participant_registry import (
    PARTICIPANT_REGISTRY_ARTIFACT_TYPE,
    PARTICIPANT_SOURCE_ROLE,
    FootballParticipantRegistry,
    ParticipantSourceReference,
    derive_participant_registry_artifact,
    load_participant_registry_artifact,
)
from sports_analytics.sports.football.schemas import football_snapshot_suite
from sports_analytics.ui.product_catalogue import (
    discover_product_read_models,
    load_product_read_model,
)
from sports_analytics.upcoming_events import (
    UPCOMING_EVENT_ARTIFACT_TYPE,
    UpcomingEvent,
    load_upcoming_event_artifact,
)


@dataclass(frozen=True, slots=True)
class PreparationResult:
    state: MVPState
    participant_registry_artifact_id: str | None
    active_competitions: tuple[str, ...]
    actions: tuple[str, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MatchSaveResult:
    validation: MatchValidation
    artifact_ids: tuple[str, ...]
    analysis_artifact_ids: tuple[str, ...]
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class OddsSaveResult:
    validation: OddsValidation
    quote_artifact_ids: tuple[str, ...]
    product_artifact_ids: tuple[str, ...]
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class _EventArtifact:
    artifact: AnalyticalArtifact
    events: tuple[UpcomingEvent, ...]


class MVPOrchestrator:
    """Inspect and advance only the allowlisted local MVP workflow."""

    def __init__(
        self,
        *,
        base_directory: Path | str | None = None,
        config_path: Path | str | None = None,
        env_file: Path | str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.base_directory = Path(base_directory or Path.cwd()).resolve()
        self.config_path = config_path
        self.env_file = env_file
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    def initialize(self) -> dict[str, Any]:
        """Idempotently initialize the exact package-native local runtime."""
        return initialize_v1(
            config_path=self.config_path,
            env_file=self.env_file,
            base_directory=self.base_directory,
        )

    def inspect(self) -> MVPStatus:
        """Read persisted state only and return one deterministic MVP status."""
        try:
            settings, paths = self._settings_paths()
            initialized = self._runtime_initialized(paths)
            if not initialized:
                facts = MVPReadinessFacts(runtime_initialized=False)
                return self._status(
                    facts,
                    blockers=("The local runtime database is not initialized.",),
                )
            snapshots = self._verified_historical_references(paths)
            registries = self._participant_registries(paths)
            champions = self._champions(paths)
            event_artifacts = self._event_artifacts(paths, registries=registries)
            quote_count = self._quote_count(paths)
            product = self._latest_product(paths)
            queue = WorkerService(paths.sqlite_path, settings.worker).get_queue_status(
                observed_at=self._now()
            )
            product_state = self._product_state(product)
            current_quote_count = (
                0
                if product_state.get("operational_state")
                in {"fair-odds-only", "no-production-champion"}
                else quote_count
            )
            facts = MVPReadinessFacts(
                runtime_initialized=True,
                historical_snapshot_count=len(snapshots),
                participant_registry_available=bool(registries),
                active_champion_count=len(champions),
                upcoming_event_count=sum(len(item.events) for item in event_artifacts),
                current_quote_count=current_quote_count,
                analysis_running=queue.running_count > 0,
                analytical_candidate_count=_count(product_state, "analytical_candidate_count"),
                held_candidate_count=_count(product_state, "held_candidate_count"),
                placeable_manual_proposal_count=_count(
                    product_state, "placeable_manual_proposal_count"
                ),
            )
            blockers = self._blockers(facts)
            model_ids = tuple(item[2] for item in champions)
            competitions = tuple(sorted({item[0] for item in champions if item[0]}))
            return MVPStatus(
                state=determine_mvp_state(facts),
                steps=setup_steps(facts),
                blockers=blockers,
                active_competitions=competitions,
                active_models=model_ids,
                historical_snapshot_count=len(snapshots),
                upcoming_event_count=facts.upcoming_event_count,
                current_quote_count=current_quote_count,
                matches_analysed=len(_list(product_state.get("events"))),
                analytical_candidate_count=facts.analytical_candidate_count,
                held_candidate_count=facts.held_candidate_count,
                rejected_candidate_count=_count(product_state, "rejected_candidate_count"),
                placeable_manual_proposal_count=(facts.placeable_manual_proposal_count),
                accumulator_count=_count(product_state, "accumulator_count"),
                last_successful_analysis=self._analysis_timestamp(product_state),
                worker_state=(
                    "running"
                    if queue.active_worker_count
                    else "starting"
                    if queue.running_count
                    else "idle"
                ),
            )
        except (SportsAnalyticsError, OSError, sqlite3.Error, ValueError) as exc:
            detail = _safe_failure(exc)
            facts = MVPReadinessFacts(
                runtime_initialized=False,
                failure=detail,
            )
            return self._status(
                facts,
                blockers=("The MVP readiness inspection failed closed.",),
                failure=detail,
            )

    def prepare_system(self) -> PreparationResult:
        """Prepare policy and participant state from verified local evidence."""
        self.initialize()
        _settings, paths = self._settings_paths()
        actions: list[str] = []
        blockers: list[str] = []
        self._ensure_default_policy(paths)
        actions.append("verified default proposal policy available")
        references = self._verified_historical_references(paths)
        registry: FootballParticipantRegistry | None = None
        if not references:
            blockers.append("No verified Football-Data historical snapshots are registered.")
        else:
            registry = self._matching_registry(paths, references)
            if registry is None:
                digest = _digest([(item.artifact_id, item.checksum_sha256) for item in references])
                relative = f"mvp/participant-registries/{digest}"
                derive_participant_registry_artifact(
                    root=paths.exports_directory,
                    source_root=paths.snapshots_directory,
                    relative_directory=relative,
                    registry_revision=f"mvp-registry-{digest[:20]}",
                    evaluated_at_utc=self._now(),
                    source_artifacts=references,
                )
                registry = load_participant_registry_artifact(
                    root=paths.exports_directory,
                    source_root=paths.snapshots_directory,
                    relative_directory=relative,
                )
                actions.append("participant registry derived from verified snapshots")
            else:
                actions.append("existing verified participant registry reused")
        if references:
            try:
                champion_report = prepare_score_champions(
                    paths=paths,
                    references=references,
                    evaluated_at_utc=self._now(),
                )
            except (SportsAnalyticsError, OSError, sqlite3.Error, ValueError) as exc:
                blockers.append(_safe_failure(exc))
            else:
                for champion in champion_report.champions:
                    action = "reused" if champion.reused else "promoted"
                    actions.append(f"{champion.competition_id} governed champion {action}")
                blockers.extend(champion_report.blockers)
        champions = self._champions(paths)
        if not champions:
            blockers.append(
                "No governance-authorized active champion exists after verified "
                "historical preparation."
            )
        else:
            actions.append("governance-authorized active champion verified")
        status = self.inspect()
        return PreparationResult(
            status.state,
            None if registry is None else registry.artifact.artifact_id,
            status.active_competitions,
            tuple(actions),
            tuple(blockers),
        )

    def enqueue_historical_data(self, *, competition: str, season: str) -> str:
        """Explicitly enqueue one allowlisted Football-Data historical import."""
        settings, paths = self._settings_paths()
        enabled_scraping = settings.scraping.model_copy(update={"enabled": True})
        job = enqueue_football_data_ingestion(
            database_path=paths.sqlite_path,
            scraping=enabled_scraping,
            competition_id=competition,
            season=season,
            actor="operator-ui",
            created_at=self._now(),
        )
        return job.id

    def validate_matches(self, rows: tuple[dict[str, object], ...]) -> MatchValidation:
        """Validate human match rows without publishing anything."""
        _settings, paths = self._settings_paths()
        registry = self._require_registry(paths)
        return validate_human_matches(
            rows,
            registry=registry,
            evaluated_at_utc=self._now(),
        )

    def save_matches(self, rows: tuple[dict[str, object], ...]) -> MatchSaveResult:
        """Publish valid matches and immediately produce fair-odds state if possible."""
        _settings, paths = self._settings_paths()
        now = self._now()
        registry = self._require_registry(paths)
        validation = validate_human_matches(
            rows,
            registry=registry,
            evaluated_at_utc=now,
        )
        if not validation.is_valid:
            return MatchSaveResult(validation, (), ())
        try:
            artifacts = publish_human_matches(
                validation,
                root=paths.exports_directory,
                registry=registry,
                evaluated_at_utc=now,
            )
        except ArtifactError as exc:
            if "already exists" not in str(exc):
                return MatchSaveResult(validation, (), (), _safe_failure(exc))
            artifacts = tuple(
                item.artifact
                for item in self._event_artifacts(paths, registries=(registry,))
                if {event.canonical_event_id for event in item.events}
                <= {event.canonical_event_id for event in validation.events}
            )
        analyses: list[str] = []
        for artifact in artifacts:
            event_artifact, events = load_upcoming_event_artifact(
                root=paths.exports_directory,
                relative_directory=artifact.relative_directory,
                expected_artifact_id=artifact.artifact_id,
                expected_checksum=artifact.checksum_sha256,
            )
            if self._champion_for(paths, events[0].competition_id) is None:
                continue
            published = self._run_analysis(
                paths=paths,
                registry=registry,
                event_artifact=event_artifact,
                events=events,
                operator_quotes=(),
                evaluated_at_utc=now,
            )
            analyses.append(published.read_model_artifact.artifact_id)
        return MatchSaveResult(
            validation,
            tuple(item.artifact_id for item in artifacts),
            tuple(analyses),
        )

    def match_options(self) -> tuple[MatchOption, ...]:
        """Return registry-backed human match choices from verified event artifacts."""
        _settings, paths = self._settings_paths()
        registry = self._require_registry(paths)
        events = tuple(
            event
            for artifact in self._event_artifacts(paths, registries=(registry,))
            for event in artifact.events
        )
        return build_match_options(events, registry=registry)

    def participant_choices(self) -> dict[str, tuple[str, ...]]:
        """Return verified competition-scoped display names for UI selectors."""
        _settings, paths = self._settings_paths()
        registry = self._require_registry(paths)
        competitions = {
            competition for item in registry.participants for competition in item.competition_ids
        }
        return {
            competition: tuple(
                sorted(
                    {
                        item.canonical_display_name
                        for item in registry.participants_for_competition(competition)
                    }
                )
            )
            for competition in sorted(competitions)
        }

    def validate_odds(self, rows: tuple[dict[str, object], ...]) -> OddsValidation:
        """Validate manual odds rows without publishing anything."""
        settings, _paths = self._settings_paths()
        return validate_human_odds(
            rows,
            match_options=self.match_options(),
            registered_provider_ids=self._provider_ids(settings),
            evaluated_at_utc=self._now(),
        )

    def save_odds(self, rows: tuple[dict[str, object], ...]) -> OddsSaveResult:
        """Publish validated offered odds and automatically refresh the product."""
        settings, paths = self._settings_paths()
        now = self._now()
        options = self.match_options()
        validation = validate_human_odds(
            rows,
            match_options=options,
            registered_provider_ids=self._provider_ids(settings),
            evaluated_at_utc=now,
        )
        if not validation.is_valid:
            return OddsSaveResult(validation, (), ())
        registry = self._require_registry(paths)
        event_artifacts = self._event_artifacts(paths, registries=(registry,))
        event_index = {
            event.canonical_event_id: item for item in event_artifacts for event in item.events
        }
        grouped: dict[str, list[OperatorQuoteInput]] = {}
        artifacts_by_id: dict[str, _EventArtifact] = {}
        for quote in validation.inputs:
            item = event_index.get(quote.canonical_event_id)
            if item is None:
                return OddsSaveResult(
                    validation,
                    (),
                    (),
                    "mismatched match: verified upcoming-event artifact is absent",
                )
            grouped.setdefault(item.artifact.artifact_id, []).append(quote)
            artifacts_by_id[item.artifact.artifact_id] = item
        quote_artifacts: list[str] = []
        products: list[str] = []
        try:
            for artifact_id in sorted(grouped):
                item = artifacts_by_id[artifact_id]
                published = self._run_analysis(
                    paths=paths,
                    registry=registry,
                    event_artifact=item.artifact,
                    events=item.events,
                    operator_quotes=tuple(grouped[artifact_id]),
                    evaluated_at_utc=now,
                )
                if published.quote_artifact is not None:
                    quote_artifacts.append(published.quote_artifact.artifact_id)
                products.append(published.read_model_artifact.artifact_id)
        except (SportsAnalyticsError, OSError, sqlite3.Error, ValueError) as exc:
            return OddsSaveResult(validation, (), (), _safe_failure(exc))
        return OddsSaveResult(
            validation,
            tuple(quote_artifacts),
            tuple(products),
        )

    def latest_product(self) -> AnalyticalArtifact | None:
        """Return the latest strictly verified product read model, if present."""
        _settings, paths = self._settings_paths()
        return self._latest_product(paths)

    def latest_proposals(self) -> AnalyticalArtifact | None:
        """Return proposals linked to the latest product without trusting raw paths."""
        product = self.latest_product()
        if product is None:
            return None
        relative = PurePosixPath(product.relative_directory).parent / "proposals"
        try:
            from sports_analytics.proposals.football import load_proposal_artifact

            return load_proposal_artifact(
                root=self._settings_paths()[1].exports_directory,
                relative_directory=relative.as_posix(),
            )
        except ArtifactError:
            return None

    def automatic_status(self) -> Any:
        """Return persisted automatic-operation state without network activity."""
        from sports_analytics.mvp.automatic_market_data import AutomaticMarketDataController

        return AutomaticMarketDataController(
            base_directory=self.base_directory,
            config_path=self.config_path,
            env_file=self.env_file,
            clock=self._clock,
        ).inspect()

    def enable_automatic_operation(
        self,
        *,
        api_key: str,
        region: str,
        competitions: tuple[str, ...],
        markets: tuple[str, ...],
        refresh_interval_minutes: int,
    ) -> Any:
        """Perform the one confirmed, bounded automatic-provider setup."""
        from sports_analytics.mvp.automatic_market_data import AutomaticMarketDataController

        return AutomaticMarketDataController(
            base_directory=self.base_directory,
            config_path=self.config_path,
            env_file=self.env_file,
            clock=self._clock,
        ).enable(
            api_key=api_key,
            region=region,
            competitions=competitions,
            markets=markets,
            refresh_interval_minutes=refresh_interval_minutes,
        )

    def pause_automatic_operation(self) -> Any:
        """Pause acquisition and cancel equivalent pending jobs."""
        from sports_analytics.mvp.automatic_market_data import AutomaticMarketDataController

        return AutomaticMarketDataController(
            base_directory=self.base_directory,
            config_path=self.config_path,
            env_file=self.env_file,
            clock=self._clock,
        ).pause()

    def resume_automatic_operation(self) -> Any:
        """Resume acquisition and enqueue one immediate durable job."""
        from sports_analytics.mvp.automatic_market_data import AutomaticMarketDataController

        return AutomaticMarketDataController(
            base_directory=self.base_directory,
            config_path=self.config_path,
            env_file=self.env_file,
            clock=self._clock,
        ).resume()

    def run_automatic_acquisition_now(self) -> str:
        """Enqueue one immediate allowlisted acquisition."""
        from sports_analytics.mvp.automatic_market_data import AutomaticMarketDataController

        return AutomaticMarketDataController(
            base_directory=self.base_directory,
            config_path=self.config_path,
            env_file=self.env_file,
            clock=self._clock,
        ).run_now()

    def replace_automatic_api_key(self, api_key: str) -> Any:
        """Validate and atomically replace the stored provider key."""
        from sports_analytics.mvp.automatic_market_data import AutomaticMarketDataController

        return AutomaticMarketDataController(
            base_directory=self.base_directory,
            config_path=self.config_path,
            env_file=self.env_file,
            clock=self._clock,
        ).replace_key(api_key)

    def run_automatic_analysis(
        self,
        *,
        settings: Settings,
        paths: RuntimePaths,
        registry: FootballParticipantRegistry,
        event_artifact: AnalyticalArtifact,
        events: tuple[UpcomingEvent, ...],
        provider_quotes: tuple[OperatorQuoteInput, ...],
        evaluated_at_utc: datetime,
    ) -> PublishedProductionFootballProduct:
        """Run the existing production product for verified provider inputs."""
        return self._run_analysis(
            paths=paths,
            registry=registry,
            event_artifact=event_artifact,
            events=events,
            operator_quotes=provider_quotes,
            evaluated_at_utc=evaluated_at_utc,
            settings=settings,
            automatic=True,
        )

    def _run_analysis(
        self,
        *,
        paths: RuntimePaths,
        registry: FootballParticipantRegistry,
        event_artifact: AnalyticalArtifact,
        events: tuple[UpcomingEvent, ...],
        operator_quotes: tuple[OperatorQuoteInput, ...],
        evaluated_at_utc: datetime,
        settings: Settings | None = None,
        automatic: bool = False,
    ) -> PublishedProductionFootballProduct:
        runtime_settings = settings or self._settings_paths()[0]
        competition = events[0].competition_id
        champion = self._champion_for(paths, competition)
        if champion is None:
            raise ConfigurationError(
                "production analysis requires a governance-authorized active champion"
            )
        policy_artifact, _policy = self._ensure_default_policy(paths)
        identity = _analysis_identity(
            event_artifact_id=event_artifact.artifact_id,
            operator_quotes=operator_quotes,
            champion_artifact_id=champion[2],
            policy_artifact_id=policy_artifact.artifact_id,
        )
        run_identity = identity[:32] if automatic else identity
        base = f"mvp/product-runs/{competition}/{run_identity}"
        read_model_relative = f"{base}/read-model"
        try:
            existing = load_analytical_artifact(
                root=paths.exports_directory,
                relative_directory=read_model_relative,
                expected_artifact_type=FOOTBALL_PRODUCT_READ_MODEL_TYPE,
                expected_schema_version=FOOTBALL_PRODUCT_READ_MODEL_SCHEMA,
            )
        except ArtifactError:
            existing = None
        if existing is not None:
            quote_artifact = self._optional_artifact(
                paths,
                f"{base}/current-quotes",
                OPERATOR_QUOTE_ARTIFACT_TYPE,
                OPERATOR_QUOTE_ARTIFACT_SCHEMA,
            )
            proposal_artifact = self._optional_proposal(paths, f"{base}/proposals")
            return PublishedProductionFootballProduct(
                existing,
                (),
                quote_artifact,
                proposal_artifact,
                None,
                None,
            )
        with connect_database(paths.sqlite_path, read_only=True) as connection:
            return run_and_publish_production_football_product(
                connection=connection,
                exports_root=paths.exports_directory,
                model_root=paths.models_directory,
                snapshots_root=paths.snapshots_directory,
                request=ProductionFootballProductRequest(
                    upcoming_event_relative_directory=event_artifact.relative_directory,
                    upcoming_event_artifact_id=event_artifact.artifact_id,
                    upcoming_event_checksum_sha256=event_artifact.checksum_sha256,
                    participant_registry_relative_directory=(registry.artifact.relative_directory),
                    participant_registry_artifact_id=registry.artifact.artifact_id,
                    participant_registry_checksum_sha256=(registry.artifact.checksum_sha256),
                    competition_id=competition,
                    market_key=champion[1],
                    evaluated_at_utc=evaluated_at_utc,
                    relative_root=base,
                    proposal_policy_relative_directory=(policy_artifact.relative_directory),
                    proposal_policy_checksum_sha256=(policy_artifact.checksum_sha256),
                    operator_quotes=operator_quotes,
                    registered_provider_ids=(
                        self._provider_ids(runtime_settings)
                        | frozenset(item.provider_id for item in operator_quotes)
                    ),
                    quote_policy=OperatorQuotePolicy(
                        maximum_age=timedelta(
                            seconds=runtime_settings.bookmakers.quote_maximum_age_seconds
                        )
                    ),
                ),
            )

    def _settings_paths(self) -> tuple[Settings, RuntimePaths]:
        settings = load_settings(
            base_directory=self.base_directory,
            config_path=self.config_path,
            env_file=self.env_file,
        )
        return settings, resolve_paths(settings, self.base_directory)

    @staticmethod
    def _runtime_initialized(paths: RuntimePaths) -> bool:
        if not paths.sqlite_path.is_file():
            return False
        status = get_migration_status(paths.sqlite_path)
        return status.is_up_to_date and status.current_version == 5

    def _verified_historical_references(
        self, paths: RuntimePaths
    ) -> tuple[ParticipantSourceReference, ...]:
        if not paths.sqlite_path.is_file():
            return ()
        with connect_database(paths.sqlite_path, read_only=True) as connection:
            records = SnapshotRepository(connection).list_snapshots(
                snapshot_type=FOOTBALL_INGESTION_SNAPSHOT_TYPE
            )
        references: list[ParticipantSourceReference] = []
        for record in records:
            if (
                record.status.value != "ready"
                or record.source_name != SOURCE_FOOTBALL_DATA_CO_UK
                or record.checksum_sha256 is None
            ):
                continue
            verified = verify_snapshot_directory(
                snapshots_directory=paths.snapshots_directory,
                relative_manifest_path=record.relative_path,
                suite=football_snapshot_suite(),
                expected_snapshot=record,
            )
            relative = PurePosixPath(verified.relative_manifest_path).parent.as_posix()
            references.append(
                ParticipantSourceReference(
                    role=PARTICIPANT_SOURCE_ROLE,
                    relative_directory=relative,
                    artifact_id=verified.snapshot_id,
                    checksum_sha256=verified.manifest_checksum_sha256,
                    artifact_type=FOOTBALL_INGESTION_SNAPSHOT_TYPE,
                    schema_version=FOOTBALL_CANONICAL_SCHEMA_VERSION,
                )
            )
        return tuple(sorted(set(references)))

    def _participant_registries(
        self, paths: RuntimePaths
    ) -> tuple[FootballParticipantRegistry, ...]:
        registries: list[FootballParticipantRegistry] = []
        for relative in _artifact_directories(
            paths.exports_directory, PARTICIPANT_REGISTRY_ARTIFACT_TYPE
        ):
            try:
                registries.append(
                    load_participant_registry_artifact(
                        root=paths.exports_directory,
                        source_root=paths.snapshots_directory,
                        relative_directory=relative,
                    )
                )
            except ArtifactError:
                continue
        return tuple(
            sorted(
                registries,
                key=lambda item: (
                    item.evaluated_at_utc,
                    item.artifact.artifact_id,
                ),
            )
        )

    def _matching_registry(
        self,
        paths: RuntimePaths,
        references: tuple[ParticipantSourceReference, ...],
    ) -> FootballParticipantRegistry | None:
        return next(
            (
                item
                for item in reversed(self._participant_registries(paths))
                if item.source_artifacts == references
            ),
            None,
        )

    def _require_registry(self, paths: RuntimePaths) -> FootballParticipantRegistry:
        registries = self._participant_registries(paths)
        if not registries:
            raise ConfigurationError(
                "prepare the verified participant registry before operator input"
            )
        return registries[-1]

    def _event_artifacts(
        self,
        paths: RuntimePaths,
        *,
        registries: tuple[FootballParticipantRegistry, ...],
    ) -> tuple[_EventArtifact, ...]:
        registry_ids = {item.artifact.artifact_id for item in registries}
        items: list[_EventArtifact] = []
        for relative in _artifact_directories(
            paths.exports_directory, UPCOMING_EVENT_ARTIFACT_TYPE
        ):
            try:
                artifact, events = load_upcoming_event_artifact(
                    root=paths.exports_directory,
                    relative_directory=relative,
                )
            except ArtifactError:
                continue
            payload = artifact.payload
            lineage = payload.get("participant_registry") if isinstance(payload, dict) else None
            if (
                isinstance(lineage, dict)
                and lineage.get("artifact_id") in registry_ids
                and all(item.event_start_utc > self._now() for item in events)
            ):
                items.append(_EventArtifact(artifact, events))
        return tuple(
            sorted(
                items,
                key=lambda item: (
                    max(event.observed_at_utc for event in item.events),
                    item.artifact.artifact_id,
                ),
            )
        )

    def _quote_count(self, paths: RuntimePaths) -> int:
        count = 0
        for relative in _artifact_directories(
            paths.exports_directory, OPERATOR_QUOTE_ARTIFACT_TYPE
        ):
            try:
                artifact = load_analytical_artifact(
                    root=paths.exports_directory,
                    relative_directory=relative,
                    expected_artifact_type=OPERATOR_QUOTE_ARTIFACT_TYPE,
                    expected_schema_version=OPERATOR_QUOTE_ARTIFACT_SCHEMA,
                )
            except ArtifactError:
                continue
            payload = artifact.payload
            if isinstance(payload, dict):
                count += len(_list(payload.get("quotes")))
        return count

    @staticmethod
    def _champions(paths: RuntimePaths) -> tuple[tuple[str, str, str], ...]:
        if not paths.sqlite_path.is_file():
            return ()
        with connect_database(paths.sqlite_path, read_only=True) as connection:
            entries = ModelGovernanceRepository(connection).list_models()
            champions: list[tuple[str, str, str]] = []
            for item in entries:
                if (
                    item.role is not ModelRole.CHAMPION
                    or item.lifecycle_status is not ModelLifecycleStatus.PROMOTED
                    or not isinstance(item.provenance, dict)
                ):
                    continue
                competition = item.provenance.get("competition_id")
                if type(competition) is not str or not competition:
                    continue
                resolved = resolve_active_score_champion(
                    connection=connection,
                    model_root=paths.models_directory,
                    competition_id=competition,
                    market_key=item.market_key,
                )
                if resolved is None:
                    raise ConfigurationError(
                        "active champion does not strictly resolve for its competition"
                    )
                champions.append(
                    (
                        competition,
                        item.market_key,
                        resolved.model_artifact_id,
                    )
                )
        return tuple(sorted(champions))

    def _champion_for(self, paths: RuntimePaths, competition: str) -> tuple[str, str, str] | None:
        return next(
            (item for item in self._champions(paths) if item[0] == competition),
            None,
        )

    def _ensure_default_policy(
        self, paths: RuntimePaths
    ) -> tuple[AnalyticalArtifact, PublishedProposalPolicy]:
        policy = PublishedProposalPolicy()
        relative = f"mvp/proposal-policies/{policy.configuration_id}"
        try:
            return load_published_proposal_policy(
                root=paths.exports_directory,
                relative_directory=relative,
            )
        except ArtifactError:
            artifact = publish_proposal_policy(
                root=paths.exports_directory,
                relative_directory=relative,
                policy=policy,
            )
            return artifact, policy

    @staticmethod
    def _provider_ids(settings: Settings) -> frozenset[str]:
        return frozenset(
            {
                settings.bookmakers.preferred_provider,
                settings.bookmakers.comparison_provider,
            }
        )

    @staticmethod
    def _latest_product(paths: RuntimePaths) -> AnalyticalArtifact | None:
        entries = tuple(
            item for item in discover_product_read_models(paths.exports_directory) if item.is_valid
        )
        loaded: list[AnalyticalArtifact] = []
        for entry in entries:
            try:
                loaded.append(
                    load_product_read_model(
                        root=paths.exports_directory,
                        entry=entry,
                    )
                )
            except ArtifactError:
                continue
        if not loaded:
            return None
        return max(
            loaded,
            key=lambda item: (
                _product_timestamp(item),
                _product_priority(item),
                item.artifact_id,
            ),
        )

    @staticmethod
    def _product_state(product: AnalyticalArtifact | None) -> dict[str, object]:
        if product is None or not isinstance(product.payload, dict):
            return {}
        value = product.payload.get("product_state")
        return cast(dict[str, object], value) if isinstance(value, dict) else {}

    @staticmethod
    def _analysis_timestamp(product_state: dict[str, object]) -> str | None:
        timestamps = [
            str(item.get("observed_at_utc"))
            for item in _list(product_state.get("events"))
            if isinstance(item, dict) and item.get("observed_at_utc")
        ]
        return max(timestamps) if timestamps else None

    @staticmethod
    def _blockers(facts: MVPReadinessFacts) -> tuple[str, ...]:
        blockers: list[str] = []
        if facts.historical_snapshot_count == 0:
            blockers.append("Verified historical data is required.")
        elif not facts.participant_registry_available:
            blockers.append("A registry derived from verified snapshots is required.")
        if facts.active_champion_count == 0:
            blockers.append("A governance-authorized active champion is required.")
        if facts.upcoming_event_count == 0:
            blockers.append("At least one verified upcoming match is required.")
        if facts.current_quote_count == 0:
            blockers.append("A complete, current offered market is required.")
        return tuple(blockers)

    @staticmethod
    def _optional_artifact(
        paths: RuntimePaths,
        relative: str,
        artifact_type: str,
        schema: str,
    ) -> AnalyticalArtifact | None:
        try:
            return load_analytical_artifact(
                root=paths.exports_directory,
                relative_directory=relative,
                expected_artifact_type=artifact_type,
                expected_schema_version=schema,
            )
        except ArtifactError:
            return None

    @staticmethod
    def _optional_proposal(paths: RuntimePaths, relative: str) -> AnalyticalArtifact | None:
        from sports_analytics.proposals.football import load_proposal_artifact

        try:
            return load_proposal_artifact(
                root=paths.exports_directory,
                relative_directory=relative,
            )
        except ArtifactError:
            return None

    def _status(
        self,
        facts: MVPReadinessFacts,
        *,
        blockers: tuple[str, ...],
        failure: str | None = None,
    ) -> MVPStatus:
        return MVPStatus(
            state=determine_mvp_state(facts),
            steps=setup_steps(facts),
            blockers=blockers,
            active_competitions=(),
            active_models=(),
            historical_snapshot_count=facts.historical_snapshot_count,
            upcoming_event_count=facts.upcoming_event_count,
            current_quote_count=facts.current_quote_count,
            matches_analysed=0,
            analytical_candidate_count=facts.analytical_candidate_count,
            held_candidate_count=facts.held_candidate_count,
            rejected_candidate_count=0,
            placeable_manual_proposal_count=(facts.placeable_manual_proposal_count),
            accumulator_count=0,
            last_successful_analysis=None,
            worker_state="unavailable",
            failure=failure,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("MVP clock must return a timezone-aware timestamp")
        return value.astimezone(UTC)


def _artifact_directories(root: Path, artifact_type: str) -> tuple[str, ...]:
    if not root.is_dir():
        return ()
    matches: list[str] = []
    manifests: list[Path] = []
    for directory, child_directories, filenames in os.walk(
        root,
        topdown=True,
        onerror=lambda _error: None,
    ):
        child_directories.sort()
        if ANALYTICAL_MANIFEST_FILENAME in filenames:
            manifests.append(Path(directory) / ANALYTICAL_MANIFEST_FILENAME)
    for manifest in manifests:
        if not manifest.is_file() or manifest.is_symlink():
            continue
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("artifact_type") == artifact_type:
            matches.append(manifest.parent.relative_to(root).as_posix())
    return tuple(sorted(set(matches)))


def _product_timestamp(artifact: AnalyticalArtifact) -> str:
    if not isinstance(artifact.payload, dict):
        return ""
    product = artifact.payload.get("product_state")
    if not isinstance(product, dict):
        return ""
    values = [
        str(item.get("observed_at_utc"))
        for item in _list(product.get("events"))
        if isinstance(item, dict) and item.get("observed_at_utc")
    ]
    return max(values) if values else ""


def _product_priority(artifact: AnalyticalArtifact) -> int:
    if not isinstance(artifact.payload, dict):
        return -1
    product = artifact.payload.get("product_state")
    if not isinstance(product, dict):
        return -1
    return {
        "no-production-champion": 0,
        "fair-odds-only": 1,
        "economic-evidence-hold": 2,
        "production-eligible": 3,
    }.get(str(product.get("operational_state")), -1)


def _count(value: dict[str, object], key: str) -> int:
    item = value.get(key, 0)
    return item if type(item) is int and item >= 0 else 0


def _list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _analysis_identity(
    *,
    event_artifact_id: str,
    operator_quotes: tuple[OperatorQuoteInput, ...],
    champion_artifact_id: str,
    policy_artifact_id: str,
) -> str:
    quote_payloads = [operator_quote_identity_payload(item) for item in operator_quotes]
    return _digest(
        {
            "event_artifact_id": event_artifact_id,
            "quote_inputs": sorted(
                quote_payloads,
                key=lambda item: json.dumps(
                    item,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
            "champion": champion_artifact_id,
            "policy": policy_artifact_id,
        }
    )


def _safe_failure(exc: BaseException) -> str:
    return sanitize_error_text(exc)[:500]
