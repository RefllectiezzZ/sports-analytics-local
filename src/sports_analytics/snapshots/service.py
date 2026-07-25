"""Publish prepared football snapshots with short SQLite transactions."""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath

from sports_analytics.core.exceptions import (
    DatabaseIntegrityError,
    SnapshotBusyError,
    SnapshotIntegrityError,
    SnapshotVerificationError,
)
from sports_analytics.data.database import connect_database, transaction
from sports_analytics.data.repositories.audit import AuditEventRepository
from sports_analytics.data.repositories.snapshots import SnapshotRepository
from sports_analytics.data.types import (
    JsonValue,
    SnapshotRecord,
    SnapshotStatus,
    normalize_uuid,
    validate_relative_snapshot_path,
)
from sports_analytics.snapshots.paths import resolve_snapshot_dir
from sports_analytics.snapshots.reader import SnapshotVerificationResult, verify_snapshot_directory
from sports_analytics.snapshots.types import PublishedSnapshot
from sports_analytics.snapshots.writer import (
    PreparedSnapshot,
    discard_prepared_snapshot,
    resolve_snapshot_directory,
)
from sports_analytics.sports.football.contracts import (
    FOOTBALL_CANONICAL_SCHEMA_VERSION,
    FOOTBALL_INGESTION_SNAPSHOT_TYPE,
    MANIFEST_FILENAME,
)


class SnapshotPublicationService:
    """Coordinate filesystem publication with SnapshotRepository metadata.

    Expensive filesystem verification never runs while holding a SQLite write
    transaction. Publication uses short BEGIN IMMEDIATE transactions only for
    metadata re-checks, atomic rename, READY transitions, and audit events.
    """

    def __init__(self, *, database_path: Path, snapshots_directory: Path) -> None:
        self._database_path = Path(database_path)
        self._snapshots_directory = Path(snapshots_directory)

    def publish_or_reuse(
        self,
        prepared: PreparedSnapshot,
        *,
        actor: str,
        correlation_id: str | None = None,
    ) -> PublishedSnapshot:
        """Publish a prepared snapshot or reuse/recover an existing one."""
        ownership_transferred = False
        try:
            existing = self._lookup_active(
                source_name=prepared.source_name,
                source_version=prepared.source_version,
                schema_version=prepared.schema_version,
            )
            if existing is not None and existing.status is SnapshotStatus.READY:
                verified = verify_snapshot_directory(
                    snapshots_directory=self._snapshots_directory,
                    relative_manifest_path=existing.relative_path,
                    expected_snapshot=existing,
                )
                ready = self._commit_ready_reuse(
                    existing=existing,
                    verified=verified,
                    prepared=prepared,
                    actor=actor,
                    correlation_id=correlation_id,
                )
                discard_prepared_snapshot(prepared)
                ownership_transferred = True
                return self._from_record(ready, prepared=prepared, reused=True, verified=verified)

            if existing is not None and existing.status is SnapshotStatus.BUILDING:
                result = self._recover_building_outside_tx(
                    existing=existing,
                    prepared=prepared,
                    actor=actor,
                    correlation_id=correlation_id,
                )
                ownership_transferred = True
                return result

            orphan = self._discover_matching_orphan(prepared)
            if orphan is not None:
                try:
                    result = self._adopt_orphan_outside_tx(
                        prepared=prepared,
                        verified=orphan,
                        actor=actor,
                        correlation_id=correlation_id,
                    )
                except SnapshotBusyError:
                    existing_after = self._lookup_active(
                        source_name=prepared.source_name,
                        source_version=prepared.source_version,
                        schema_version=prepared.schema_version,
                    )
                    if (
                        existing_after is not None
                        and existing_after.status is SnapshotStatus.READY
                    ):
                        verified = verify_snapshot_directory(
                            snapshots_directory=self._snapshots_directory,
                            relative_manifest_path=existing_after.relative_path,
                            expected_snapshot=existing_after,
                        )
                        ready = self._commit_ready_reuse(
                            existing=existing_after,
                            verified=verified,
                            prepared=prepared,
                            actor=actor,
                            correlation_id=correlation_id,
                        )
                        discard_prepared_snapshot(prepared)
                        ownership_transferred = True
                        return self._from_record(
                            ready, prepared=prepared, reused=True, verified=verified
                        )
                    raise
                ownership_transferred = True
                return result

            final_directory = resolve_snapshot_directory(
                self._snapshots_directory,
                prepared.relative_directory,
            )
            if final_directory.exists():
                # Exact prepared path exists but identity discovery found no match.
                discard_prepared_snapshot(prepared)
                ownership_transferred = True
                msg = (
                    f"conflicting snapshot directory at {prepared.relative_directory}; "
                    "refusing to overwrite"
                )
                raise SnapshotIntegrityError(msg)

            try:
                published = self._publish_new(
                    prepared, actor=actor, correlation_id=correlation_id
                )
            except SnapshotBusyError:
                # Another writer may have finished READY between lookup and insert.
                existing_after = self._lookup_active(
                    source_name=prepared.source_name,
                    source_version=prepared.source_version,
                    schema_version=prepared.schema_version,
                )
                if existing_after is not None and existing_after.status is SnapshotStatus.READY:
                    verified = verify_snapshot_directory(
                        snapshots_directory=self._snapshots_directory,
                        relative_manifest_path=existing_after.relative_path,
                        expected_snapshot=existing_after,
                    )
                    ready = self._commit_ready_reuse(
                        existing=existing_after,
                        verified=verified,
                        prepared=prepared,
                        actor=actor,
                        correlation_id=correlation_id,
                    )
                    discard_prepared_snapshot(prepared)
                    ownership_transferred = True
                    return self._from_record(
                        ready, prepared=prepared, reused=True, verified=verified
                    )
                if (
                    existing_after is not None
                    and existing_after.status is SnapshotStatus.BUILDING
                ):
                    raise
                raise
            ownership_transferred = True
            return published
        except DatabaseIntegrityError as exc:
            discard_prepared_snapshot(prepared)
            ownership_transferred = True
            existing = self._lookup_active(
                source_name=prepared.source_name,
                source_version=prepared.source_version,
                schema_version=prepared.schema_version,
            )
            if existing is not None and existing.status is SnapshotStatus.READY:
                verified = verify_snapshot_directory(
                    snapshots_directory=self._snapshots_directory,
                    relative_manifest_path=existing.relative_path,
                    expected_snapshot=existing,
                )
                return self._from_record(
                    existing, prepared=prepared, reused=True, verified=verified
                )
            if existing is not None and existing.status is SnapshotStatus.BUILDING:
                msg = "active snapshot build in progress for source version"
                raise SnapshotBusyError(msg) from exc
            raise
        finally:
            if not ownership_transferred:
                discard_prepared_snapshot(prepared)

    def _lookup_active(
        self,
        *,
        source_name: str,
        source_version: str,
        schema_version: str,
    ) -> SnapshotRecord | None:
        with connect_database(self._database_path, read_only=True) as connection:
            return SnapshotRepository(connection).get_active_snapshot_by_source_version(
                snapshot_type=FOOTBALL_INGESTION_SNAPSHOT_TYPE,
                source_name=source_name,
                source_version=source_version,
                schema_version=schema_version,
            )

    def _commit_ready_reuse(
        self,
        *,
        existing: SnapshotRecord,
        verified: SnapshotVerificationResult,
        prepared: PreparedSnapshot,
        actor: str,
        correlation_id: str | None,
    ) -> SnapshotRecord:
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                snapshots = SnapshotRepository(connection)
                audit = AuditEventRepository(connection)
                current = snapshots.get_snapshot(existing.id)
                if (
                    current is None
                    or current.status is not SnapshotStatus.READY
                    or current.version != existing.version
                    or current.source_name != prepared.source_name
                    or current.source_version != prepared.source_version
                    or current.schema_version != prepared.schema_version
                    or current.relative_path != existing.relative_path
                    or current.checksum_sha256 != verified.manifest_checksum_sha256
                ):
                    msg = "READY snapshot changed during reuse confirmation"
                    raise SnapshotBusyError(msg)
                audit.append_event(
                    event_type="ingestion.snapshot-reused",
                    entity_type="snapshot",
                    entity_id=current.id,
                    actor=actor,
                    correlation_id=correlation_id,
                    details=self._audit_details(
                        prepared, reused=True, snapshot_id=current.id, verified=verified
                    ),
                    occurred_at=prepared.source_observed_at_utc,
                )
                return current

    def _recover_building_outside_tx(
        self,
        *,
        existing: SnapshotRecord,
        prepared: PreparedSnapshot,
        actor: str,
        correlation_id: str | None,
    ) -> PublishedSnapshot:
        # Derive the crashed publication path from the EXISTING row, never from
        # the newly prepared random UUID directory.
        relative_directory = _directory_from_manifest_path(existing.relative_path)
        try:
            existing_directory = resolve_snapshot_dir(
                self._snapshots_directory,
                relative_directory,
            )
        except SnapshotVerificationError:
            discard_prepared_snapshot(prepared)
            msg = (
                "incomplete snapshot publication: BUILDING metadata exists without a "
                "final directory; retry later"
            )
            raise SnapshotBusyError(msg) from None

        if not existing_directory.exists():
            discard_prepared_snapshot(prepared)
            msg = (
                "incomplete snapshot publication: BUILDING metadata exists without a "
                "final directory; retry later"
            )
            raise SnapshotBusyError(msg)

        verified = verify_snapshot_directory(
            snapshots_directory=self._snapshots_directory,
            relative_manifest_path=existing.relative_path,
        )
        self._assert_verified_identity(verified, prepared)

        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                snapshots = SnapshotRepository(connection)
                audit = AuditEventRepository(connection)
                current = snapshots.get_snapshot(existing.id)
                if (
                    current is None
                    or current.status is not SnapshotStatus.BUILDING
                    or current.version != existing.version
                    or current.source_name != prepared.source_name
                    or current.source_version != prepared.source_version
                    or current.schema_version != prepared.schema_version
                    or current.relative_path != existing.relative_path
                ):
                    msg = "BUILDING snapshot changed during recovery confirmation"
                    raise SnapshotBusyError(msg)
                ready = snapshots.mark_snapshot_ready(
                    current.id,
                    checksum_sha256=verified.manifest_checksum_sha256,
                    row_count=verified.games_count,
                    expected_version=current.version,
                    ready_at=verified.source_observed_at_utc or prepared.source_observed_at_utc,
                )
                audit.append_event(
                    event_type="ingestion.snapshot-created",
                    entity_type="snapshot",
                    entity_id=ready.id,
                    actor=actor,
                    correlation_id=correlation_id,
                    details=self._audit_details(
                        prepared, reused=False, snapshot_id=ready.id, verified=verified
                    ),
                    occurred_at=verified.source_observed_at_utc
                    or prepared.source_observed_at_utc,
                )
        discard_prepared_snapshot(prepared)
        return self._from_record(ready, prepared=prepared, reused=False, verified=verified)

    def _discover_matching_orphan(
        self,
        prepared: PreparedSnapshot,
    ) -> SnapshotVerificationResult | None:
        """Bounded orphan discovery under competition/season parent only.

        Inspects only direct child directories whose names are canonical UUIDs.
        A candidate is adopted only after full verification and exact source
        identity match. A different manifest checksum from ``prepared`` is
        allowed after identity verification because snapshot UUID / PyArrow /
        environment metadata may differ; values are taken from the orphan
        manifest, never mixed with prepared identity.
        """
        parent_relative = PurePosixPath(
            FOOTBALL_INGESTION_SNAPSHOT_TYPE,
            FOOTBALL_CANONICAL_SCHEMA_VERSION,
            prepared.competition_id,
            _season_label_from_season_id(prepared.season_id),
        ).as_posix()
        parent_relative = validate_relative_snapshot_path(parent_relative)
        try:
            parent = resolve_snapshot_dir(self._snapshots_directory, parent_relative)
        except SnapshotVerificationError:
            return None
        if not parent.exists() or not parent.is_dir():
            return None

        matches: list[SnapshotVerificationResult] = []
        for child in sorted(parent.iterdir(), key=lambda path: path.name):
            if child.is_symlink():
                continue
            if not child.is_dir():
                continue
            try:
                normalize_uuid(child.name)
            except Exception:
                continue
            relative_manifest = validate_relative_snapshot_path(
                PurePosixPath(parent_relative, child.name, MANIFEST_FILENAME).as_posix()
            )
            try:
                verified = verify_snapshot_directory(
                    snapshots_directory=self._snapshots_directory,
                    relative_manifest_path=relative_manifest,
                )
            except (SnapshotVerificationError, SnapshotIntegrityError):
                continue
            if self._identity_matches(verified, prepared):
                matches.append(verified)

        if len(matches) == 0:
            return None
        if len(matches) > 1:
            msg = "multiple identity-matching orphan snapshot directories found"
            raise SnapshotIntegrityError(msg)
        return matches[0]

    def _adopt_orphan_outside_tx(
        self,
        *,
        prepared: PreparedSnapshot,
        verified: SnapshotVerificationResult,
        actor: str,
        correlation_id: str | None,
    ) -> PublishedSnapshot:
        relative_manifest = verified.relative_manifest_path
        metadata = verified.metadata if verified.metadata is not None else prepared.metadata
        observed = verified.source_observed_at_utc or prepared.source_observed_at_utc
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                snapshots = SnapshotRepository(connection)
                audit = AuditEventRepository(connection)
                active = snapshots.get_active_snapshot_by_source_version(
                    snapshot_type=FOOTBALL_INGESTION_SNAPSHOT_TYPE,
                    source_name=prepared.source_name,
                    source_version=prepared.source_version,
                    schema_version=prepared.schema_version,
                )
                if active is not None:
                    msg = "active snapshot appeared during orphan adoption"
                    raise SnapshotBusyError(msg)
                record = snapshots.create_building_snapshot(
                    snapshot_type=FOOTBALL_INGESTION_SNAPSHOT_TYPE,
                    relative_path=relative_manifest,
                    source_name=verified.source_name or prepared.source_name,
                    schema_version=verified.schema_version or prepared.schema_version,
                    metadata=metadata,
                    snapshot_id=verified.snapshot_id,
                    source_version=verified.source_version or prepared.source_version,
                    created_at=observed,
                )
                ready = snapshots.mark_snapshot_ready(
                    record.id,
                    checksum_sha256=verified.manifest_checksum_sha256,
                    row_count=verified.games_count,
                    expected_version=record.version,
                    ready_at=observed,
                )
                audit.append_event(
                    event_type="ingestion.snapshot-created",
                    entity_type="snapshot",
                    entity_id=ready.id,
                    actor=actor,
                    correlation_id=correlation_id,
                    details=self._audit_details(
                        prepared, reused=False, snapshot_id=ready.id, verified=verified
                    ),
                    occurred_at=observed,
                )
        discard_prepared_snapshot(prepared)
        return self._from_record(ready, prepared=prepared, reused=False, verified=verified)

    def _publish_new(
        self,
        prepared: PreparedSnapshot,
        *,
        actor: str,
        correlation_id: str | None,
    ) -> PublishedSnapshot:
        final_directory = resolve_snapshot_directory(
            self._snapshots_directory,
            prepared.relative_directory,
        )
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                snapshots = SnapshotRepository(connection)
                audit = AuditEventRepository(connection)
                active = snapshots.get_active_snapshot_by_source_version(
                    snapshot_type=FOOTBALL_INGESTION_SNAPSHOT_TYPE,
                    source_name=prepared.source_name,
                    source_version=prepared.source_version,
                    schema_version=prepared.schema_version,
                )
                if active is not None:
                    msg = "active snapshot appeared during new publication"
                    raise SnapshotBusyError(msg)
                if final_directory.exists():
                    msg = (
                        f"conflicting snapshot directory at {prepared.relative_directory}; "
                        "refusing to overwrite"
                    )
                    raise SnapshotIntegrityError(msg)
                record = snapshots.create_building_snapshot(
                    snapshot_type=FOOTBALL_INGESTION_SNAPSHOT_TYPE,
                    relative_path=prepared.relative_manifest_path,
                    source_name=prepared.source_name,
                    schema_version=prepared.schema_version,
                    metadata=prepared.metadata,
                    snapshot_id=prepared.snapshot_id,
                    source_version=prepared.source_version,
                    created_at=prepared.source_observed_at_utc,
                )
                try:
                    final_directory.parent.mkdir(parents=True, exist_ok=True)
                    prepared.temporary_directory.rename(final_directory)
                except OSError as exc:
                    msg = "failed to publish snapshot directory"
                    raise SnapshotIntegrityError(msg) from exc
                ready = snapshots.mark_snapshot_ready(
                    record.id,
                    checksum_sha256=prepared.manifest_checksum_sha256,
                    row_count=prepared.games_count,
                    expected_version=record.version,
                    ready_at=prepared.source_observed_at_utc,
                )
                audit.append_event(
                    event_type="ingestion.snapshot-created",
                    entity_type="snapshot",
                    entity_id=ready.id,
                    actor=actor,
                    correlation_id=correlation_id,
                    details=self._audit_details(prepared, reused=False, snapshot_id=ready.id),
                    occurred_at=prepared.source_observed_at_utc,
                )
                return self._from_record(ready, prepared=prepared, reused=False)

    @staticmethod
    def _identity_matches(verified: SnapshotVerificationResult, prepared: PreparedSnapshot) -> bool:
        """Exact ingestion identity match for BUILDING recovery and orphan adoption.

        A different manifest checksum from ``prepared`` is not automatic
        corruption: snapshot UUID, environment metadata, or supported PyArrow
        version may differ. Adoption still requires complete identity and file
        verification, and all published metadata is taken from the verified
        orphan/BUILDING manifest rather than mixed with prepared values.
        """
        return (
            verified.manifest_version == prepared.manifest_version
            and verified.snapshot_type == prepared.snapshot_type
            and verified.schema_version == prepared.schema_version
            and verified.source_name == prepared.source_name
            and verified.source_version == prepared.source_version
            and verified.source_competition_code == prepared.source_competition_code
            and verified.source_season_code == prepared.source_season_code
            and verified.competition_id == prepared.competition_id
            and verified.season_id == prepared.season_id
            and verified.source_file_sha256 == prepared.source_file_sha256
        )

    @staticmethod
    def _assert_verified_identity(
        verified: SnapshotVerificationResult,
        prepared: PreparedSnapshot,
    ) -> None:
        if not SnapshotPublicationService._identity_matches(verified, prepared):
            msg = "existing BUILDING snapshot identity does not match requested ingestion"
            raise SnapshotIntegrityError(msg)

    @staticmethod
    def _audit_details(
        prepared: PreparedSnapshot,
        *,
        reused: bool,
        snapshot_id: str,
        verified: SnapshotVerificationResult | None = None,
    ) -> dict[str, JsonValue]:
        return {
            "snapshot_id": snapshot_id,
            "source_name": (
                verified.source_name
                if verified is not None and verified.source_name is not None
                else prepared.source_name
            ),
            "source_version": (
                verified.source_version
                if verified is not None and verified.source_version is not None
                else prepared.source_version
            ),
            "competition_id": (
                verified.competition_id
                if verified is not None and verified.competition_id is not None
                else prepared.competition_id
            ),
            "season_id": (
                verified.season_id
                if verified is not None and verified.season_id is not None
                else prepared.season_id
            ),
            "games_count": (
                verified.games_count if verified is not None else prepared.games_count
            ),
            "odds_count": (
                verified.odds_quotes_count
                if verified is not None and verified.odds_quotes_count is not None
                else prepared.odds_quotes_count
            ),
            "statistics_count": (
                verified.statistics_rows_count
                if verified is not None and verified.statistics_rows_count is not None
                else prepared.statistics_rows_count
            ),
            "reused": reused,
        }

    @staticmethod
    def _from_record(
        record: SnapshotRecord,
        *,
        prepared: PreparedSnapshot,
        reused: bool,
        verified: SnapshotVerificationResult | None = None,
    ) -> PublishedSnapshot:
        return PublishedSnapshot(
            snapshot_id=record.id,
            snapshot_status=record.status,
            snapshot_reused=reused,
            snapshot_relative_path=record.relative_path,
            source_name=record.source_name,
            source_version=(
                record.source_version
                or (verified.source_version if verified is not None else None)
                or prepared.source_version
            ),
            source_file_sha256=(
                verified.source_file_sha256
                if verified is not None and verified.source_file_sha256 is not None
                else prepared.source_file_sha256
            ),
            competition_id=(
                verified.competition_id
                if verified is not None and verified.competition_id is not None
                else prepared.competition_id
            ),
            season_id=(
                verified.season_id
                if verified is not None and verified.season_id is not None
                else prepared.season_id
            ),
            games_count=(
                record.row_count
                if record.row_count is not None
                else verified.games_count
                if verified is not None
                else prepared.games_count
            ),
            teams_count=(
                verified.teams_count
                if verified is not None and verified.teams_count is not None
                else prepared.teams_count
            ),
            odds_quotes_count=(
                verified.odds_quotes_count
                if verified is not None and verified.odds_quotes_count is not None
                else prepared.odds_quotes_count
            ),
            statistics_rows_count=(
                verified.statistics_rows_count
                if verified is not None and verified.statistics_rows_count is not None
                else prepared.statistics_rows_count
            ),
            duplicate_rows_discarded=(
                verified.duplicate_rows_discarded
                if verified is not None and verified.duplicate_rows_discarded is not None
                else prepared.duplicate_rows_discarded
            ),
            warnings_count=(
                verified.warnings_count
                if verified is not None and verified.warnings_count is not None
                else prepared.warnings_count
            ),
            manifest_checksum_sha256=(
                record.checksum_sha256
                or (verified.manifest_checksum_sha256 if verified is not None else None)
                or prepared.manifest_checksum_sha256
            ),
            source_observed_at_utc=(
                verified.source_observed_at_utc
                if verified is not None and verified.source_observed_at_utc is not None
                else prepared.source_observed_at_utc
            ),
        )


def _directory_from_manifest_path(relative_manifest_path: str) -> str:
    validated = validate_relative_snapshot_path(relative_manifest_path)
    path = PurePosixPath(validated)
    if path.name != MANIFEST_FILENAME or path.parent == PurePosixPath("."):
        msg = "BUILDING relative_path must point to manifest.json under a snapshot directory"
        raise SnapshotIntegrityError(msg)
    return validate_relative_snapshot_path(path.parent.as_posix())


def _season_label_from_season_id(season_id: str) -> str:
    # season_id is "{competition_id}:{YYYY-YYYY}"
    if ":" not in season_id:
        msg = "season_id missing canonical label"
        raise SnapshotIntegrityError(msg)
    return season_id.rsplit(":", 1)[-1]


def cleanup_temp_directory(path: Path) -> None:
    """Best-effort temporary directory cleanup that preserves primary exceptions."""
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass
