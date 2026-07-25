"""Publish prepared snapshots with short SQLite transactions (sport-agnostic)."""

from __future__ import annotations

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
from sports_analytics.snapshots.spec import (
    MANIFEST_FILENAME,
    SnapshotDatasetSuite,
    SnapshotMetrics,
)
from sports_analytics.snapshots.types import PublishedSnapshot
from sports_analytics.snapshots.writer import (
    PreparedSnapshot,
    discard_prepared_snapshot,
    resolve_snapshot_directory,
)


class SnapshotPublicationService:
    """Coordinate filesystem publication with SnapshotRepository metadata.

    Expensive filesystem verification never runs while holding a SQLite write
    transaction. Publication uses short BEGIN IMMEDIATE transactions only for
    metadata re-checks, the same-filesystem atomic rename, READY transitions, and
    audit events.

    The service is domain-neutral: every sport-specific fact arrives through the
    validated snapshot identity and dataset suite.
    """

    def __init__(
        self,
        *,
        database_path: Path,
        snapshots_directory: Path,
        suite: SnapshotDatasetSuite,
    ) -> None:
        self._database_path = Path(database_path)
        self._snapshots_directory = Path(snapshots_directory)
        self._suite = suite

    def publish_or_reuse(
        self,
        prepared: PreparedSnapshot,
        *,
        actor: str,
        correlation_id: str | None = None,
    ) -> PublishedSnapshot:
        """Publish a prepared snapshot or reuse/recover/adopt an existing one."""
        if prepared.suite is not self._suite and not _suites_equivalent(
            prepared.suite, self._suite
        ):
            msg = "prepared snapshot dataset suite does not match the publication service"
            raise SnapshotIntegrityError(msg)
        ownership_transferred = False
        try:
            existing = self._lookup_active(prepared)
            if existing is not None and existing.status is SnapshotStatus.READY:
                result = self._reuse_ready_outside_tx(
                    existing=existing,
                    prepared=prepared,
                    actor=actor,
                    correlation_id=correlation_id,
                )
                ownership_transferred = True
                return result

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
                    ready = self._reuse_ready_after_race(
                        prepared=prepared,
                        actor=actor,
                        correlation_id=correlation_id,
                    )
                    if ready is None:
                        raise
                    ownership_transferred = True
                    return ready
                ownership_transferred = True
                return result

            final_directory = resolve_snapshot_directory(
                self._snapshots_directory,
                prepared.relative_directory,
            )
            if final_directory.exists():
                # The exact prepared path exists but identity discovery found no match.
                discard_prepared_snapshot(prepared)
                ownership_transferred = True
                msg = (
                    f"conflicting snapshot directory at {prepared.relative_directory}; "
                    "refusing to overwrite"
                )
                raise SnapshotIntegrityError(msg)

            try:
                published = self._publish_new(
                    prepared,
                    actor=actor,
                    correlation_id=correlation_id,
                )
            except SnapshotBusyError:
                ready = self._reuse_ready_after_race(
                    prepared=prepared,
                    actor=actor,
                    correlation_id=correlation_id,
                )
                if ready is None:
                    raise
                ownership_transferred = True
                return ready
            ownership_transferred = True
            return published
        except DatabaseIntegrityError as exc:
            discard_prepared_snapshot(prepared)
            ownership_transferred = True
            existing = self._lookup_active(prepared)
            if existing is not None and existing.status is SnapshotStatus.READY:
                verified = self._verify_existing(existing)
                return self._from_record(
                    existing,
                    prepared=prepared,
                    reused=True,
                    verified=verified,
                )
            if existing is not None and existing.status is SnapshotStatus.BUILDING:
                msg = "active snapshot build in progress for source version"
                raise SnapshotBusyError(msg) from exc
            raise
        finally:
            if not ownership_transferred:
                discard_prepared_snapshot(prepared)

    def discard_prepared(self, prepared: PreparedSnapshot) -> None:
        """Remove a temporary prepared directory that was never published."""
        discard_prepared_snapshot(prepared)

    def _lookup_active(self, prepared: PreparedSnapshot) -> SnapshotRecord | None:
        with connect_database(self._database_path, read_only=True) as connection:
            return SnapshotRepository(connection).get_active_snapshot_by_source_version(
                snapshot_type=prepared.snapshot_type,
                source_name=prepared.source_name,
                source_version=prepared.source_version,
                schema_version=prepared.schema_version,
            )

    def _verify_existing(
        self,
        existing: SnapshotRecord,
        *,
        expect_record: bool = True,
    ) -> SnapshotVerificationResult:
        return verify_snapshot_directory(
            snapshots_directory=self._snapshots_directory,
            relative_manifest_path=existing.relative_path,
            suite=self._suite,
            expected_snapshot=existing if expect_record else None,
        )

    def _reuse_ready_outside_tx(
        self,
        *,
        existing: SnapshotRecord,
        prepared: PreparedSnapshot,
        actor: str,
        correlation_id: str | None,
    ) -> PublishedSnapshot:
        verified = self._verify_existing(existing)
        self._assert_verified_identity(verified, prepared, context="READY snapshot")
        ready = self._commit_ready_reuse(
            existing=existing,
            verified=verified,
            prepared=prepared,
            actor=actor,
            correlation_id=correlation_id,
        )
        discard_prepared_snapshot(prepared)
        return self._from_record(ready, prepared=prepared, reused=True, verified=verified)

    def _reuse_ready_after_race(
        self,
        *,
        prepared: PreparedSnapshot,
        actor: str,
        correlation_id: str | None,
    ) -> PublishedSnapshot | None:
        """Reuse a READY snapshot that another writer completed concurrently."""
        existing = self._lookup_active(prepared)
        if existing is None or existing.status is not SnapshotStatus.READY:
            return None
        return self._reuse_ready_outside_tx(
            existing=existing,
            prepared=prepared,
            actor=actor,
            correlation_id=correlation_id,
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
                    or current.snapshot_type != prepared.snapshot_type
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
                        prepared,
                        reused=True,
                        snapshot_id=current.id,
                        verified=verified,
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
        busy_message = (
            "incomplete snapshot publication: BUILDING metadata exists without a "
            "final directory; retry later"
        )
        try:
            relative_directory = _directory_from_manifest_path(existing.relative_path)
            existing_directory = resolve_snapshot_dir(
                self._snapshots_directory,
                relative_directory,
            )
        except (SnapshotVerificationError, SnapshotIntegrityError):
            discard_prepared_snapshot(prepared)
            raise SnapshotBusyError(busy_message) from None

        if not existing_directory.exists():
            discard_prepared_snapshot(prepared)
            raise SnapshotBusyError(busy_message)

        verified = self._verify_existing(existing, expect_record=False)
        self._assert_verified_identity(verified, prepared, context="BUILDING snapshot")

        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                snapshots = SnapshotRepository(connection)
                audit = AuditEventRepository(connection)
                current = snapshots.get_snapshot(existing.id)
                if (
                    current is None
                    or current.status is not SnapshotStatus.BUILDING
                    or current.version != existing.version
                    or current.snapshot_type != prepared.snapshot_type
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
                    row_count=verified.primary_row_count,
                    expected_version=current.version,
                    ready_at=verified.source_observed_at_utc,
                )
                audit.append_event(
                    event_type="ingestion.snapshot-created",
                    entity_type="snapshot",
                    entity_id=ready.id,
                    actor=actor,
                    correlation_id=correlation_id,
                    details=self._audit_details(
                        prepared,
                        reused=False,
                        snapshot_id=ready.id,
                        verified=verified,
                    ),
                    occurred_at=verified.source_observed_at_utc,
                )
        discard_prepared_snapshot(prepared)
        return self._from_record(ready, prepared=prepared, reused=False, verified=verified)

    def _discover_matching_orphan(
        self,
        prepared: PreparedSnapshot,
    ) -> SnapshotVerificationResult | None:
        """Bounded orphan discovery under the identity's partition parent only.

        Inspects only direct child directories whose names are canonical UUIDs.
        A candidate is adopted only after full verification and an exact identity
        match. A different manifest checksum from ``prepared`` is allowed after
        identity verification because snapshot UUID, environment metadata, or the
        supported PyArrow version may differ; every published value is then taken
        from the verified orphan manifest and never mixed with prepared values.
        """
        parent_relative = prepared.identity.relative_parent_directory()
        try:
            parent = resolve_snapshot_dir(self._snapshots_directory, parent_relative)
        except SnapshotVerificationError:
            return None
        if not parent.exists() or not parent.is_dir():
            return None

        matches: list[SnapshotVerificationResult] = []
        for child in sorted(parent.iterdir(), key=lambda path: path.name):
            if child.is_symlink() or not child.is_dir():
                continue
            try:
                normalized = normalize_uuid(child.name)
            except Exception:  # noqa: BLE001 - non-UUID children are not project-owned
                continue
            if normalized != child.name:
                continue
            relative_manifest = validate_relative_snapshot_path(
                PurePosixPath(parent_relative, child.name, MANIFEST_FILENAME).as_posix()
            )
            try:
                verified = verify_snapshot_directory(
                    snapshots_directory=self._snapshots_directory,
                    relative_manifest_path=relative_manifest,
                    suite=self._suite,
                )
            except (SnapshotVerificationError, SnapshotIntegrityError):
                # Malformed candidates are deterministically ignored, never adopted.
                continue
            if self._identity_matches(verified, prepared):
                matches.append(verified)

        if not matches:
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
        with connect_database(self._database_path) as connection:
            with transaction(connection, immediate=True):
                snapshots = SnapshotRepository(connection)
                audit = AuditEventRepository(connection)
                active = snapshots.get_active_snapshot_by_source_version(
                    snapshot_type=verified.snapshot_type,
                    source_name=verified.source_name,
                    source_version=verified.source_version,
                    schema_version=verified.schema_version,
                )
                if active is not None:
                    msg = "active snapshot appeared during orphan adoption"
                    raise SnapshotBusyError(msg)
                record = snapshots.create_building_snapshot(
                    snapshot_type=verified.snapshot_type,
                    relative_path=verified.relative_manifest_path,
                    source_name=verified.source_name,
                    schema_version=verified.schema_version,
                    metadata=_verified_metadata(verified),
                    snapshot_id=verified.snapshot_id,
                    source_version=verified.source_version,
                    created_at=verified.source_observed_at_utc,
                )
                ready = snapshots.mark_snapshot_ready(
                    record.id,
                    checksum_sha256=verified.manifest_checksum_sha256,
                    row_count=verified.primary_row_count,
                    expected_version=record.version,
                    ready_at=verified.source_observed_at_utc,
                )
                audit.append_event(
                    event_type="ingestion.snapshot-created",
                    entity_type="snapshot",
                    entity_id=ready.id,
                    actor=actor,
                    correlation_id=correlation_id,
                    details=self._audit_details(
                        prepared,
                        reused=False,
                        snapshot_id=ready.id,
                        verified=verified,
                    ),
                    occurred_at=verified.source_observed_at_utc,
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
                    snapshot_type=prepared.snapshot_type,
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
                    snapshot_type=prepared.snapshot_type,
                    relative_path=prepared.relative_manifest_path,
                    source_name=prepared.source_name,
                    schema_version=prepared.schema_version,
                    metadata=_prepared_metadata(prepared),
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
                    row_count=prepared.primary_row_count,
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
    def _identity_matches(
        verified: SnapshotVerificationResult,
        prepared: PreparedSnapshot,
    ) -> bool:
        """Exact snapshot identity match for BUILDING recovery and orphan adoption."""
        return (
            verified.manifest_version == prepared.manifest_version
            and verified.snapshot_type == prepared.snapshot_type
            and verified.schema_version == prepared.schema_version
            and verified.source_name == prepared.source_name
            and verified.source_version == prepared.source_version
            and verified.partition_keys == tuple(sorted(prepared.partition_keys))
            and verified.raw_artifact_sha256 == prepared.raw_artifact_sha256
            and tuple(name for name, _count in verified.row_counts) == prepared.suite.dataset_names
        )

    @staticmethod
    def _assert_verified_identity(
        verified: SnapshotVerificationResult,
        prepared: PreparedSnapshot,
        *,
        context: str,
    ) -> None:
        if not SnapshotPublicationService._identity_matches(verified, prepared):
            msg = f"existing {context} identity does not match the requested ingestion"
            raise SnapshotIntegrityError(msg)

    @staticmethod
    def _audit_details(
        prepared: PreparedSnapshot,
        *,
        reused: bool,
        snapshot_id: str,
        verified: SnapshotVerificationResult | None = None,
    ) -> dict[str, JsonValue]:
        row_counts = verified.row_counts if verified is not None else prepared.metrics.row_counts
        details: dict[str, JsonValue] = {
            "snapshot_id": snapshot_id,
            "snapshot_type": prepared.snapshot_type,
            "schema_version": prepared.schema_version,
            "source_name": prepared.source_name,
            "source_version": prepared.source_version,
            "reused": reused,
            "row_counts": {name: count for name, count in row_counts},
        }
        for key, value in prepared.partition_keys:
            details[key] = value
        return details

    @staticmethod
    def _from_record(
        record: SnapshotRecord,
        *,
        prepared: PreparedSnapshot,
        reused: bool,
        verified: SnapshotVerificationResult | None = None,
    ) -> PublishedSnapshot:
        if verified is not None:
            metrics = SnapshotMetrics(
                row_counts=verified.row_counts,
                file_count=verified.file_count,
                byte_count=verified.byte_count,
                quality_summary=verified.quality_summary,
                warnings_count=verified.warnings_count,
            )
            partition_keys = verified.partition_keys
            raw_sha = verified.raw_artifact_sha256
            domain_metadata = dict(verified.domain_metadata)
            observed = verified.source_observed_at_utc
            source_version = verified.source_version
        else:
            metrics = prepared.metrics
            partition_keys = prepared.partition_keys
            raw_sha = prepared.raw_artifact_sha256
            domain_metadata = dict(prepared.domain_metadata)
            observed = prepared.source_observed_at_utc
            source_version = prepared.source_version
        return PublishedSnapshot(
            snapshot_id=record.id,
            snapshot_status=record.status,
            snapshot_reused=reused,
            snapshot_relative_path=record.relative_path,
            snapshot_type=record.snapshot_type,
            schema_version=record.schema_version,
            source_name=record.source_name,
            source_version=record.source_version or source_version,
            raw_artifact_sha256=raw_sha,
            manifest_checksum_sha256=(record.checksum_sha256 or prepared.manifest_checksum_sha256),
            partition_keys=partition_keys,
            metrics=metrics,
            domain_metadata=domain_metadata,
            source_observed_at_utc=observed,
        )


def _prepared_metadata(prepared: PreparedSnapshot) -> dict[str, JsonValue]:
    metadata: dict[str, JsonValue] = dict(prepared.domain_metadata)
    for key, value in prepared.partition_keys:
        metadata[key] = value
    metadata["row_counts"] = {name: count for name, count in prepared.metrics.row_counts}
    return metadata


def _verified_metadata(verified: SnapshotVerificationResult) -> dict[str, JsonValue]:
    metadata: dict[str, JsonValue] = dict(verified.domain_metadata)
    for key, value in verified.partition_keys:
        metadata[key] = value
    metadata["row_counts"] = {name: count for name, count in verified.row_counts}
    return metadata


def _directory_from_manifest_path(relative_manifest_path: str) -> str:
    validated = validate_relative_snapshot_path(relative_manifest_path)
    path = PurePosixPath(validated)
    if path.name != MANIFEST_FILENAME or path.parent == PurePosixPath("."):
        msg = "BUILDING relative_path must point to manifest.json under a snapshot directory"
        raise SnapshotIntegrityError(msg)
    return path.parent.as_posix()


def _suites_equivalent(left: SnapshotDatasetSuite, right: SnapshotDatasetSuite) -> bool:
    """Return whether two suites declare the same ordered descriptors and primary."""
    if left.primary_dataset_name != right.primary_dataset_name:
        return False
    if len(left.descriptors) != len(right.descriptors):
        return False
    for left_item, right_item in zip(left.descriptors, right.descriptors, strict=True):
        if left_item.dataset_name != right_item.dataset_name:
            return False
        if left_item.relative_filename != right_item.relative_filename:
            return False
        if left_item.schema_fingerprint != right_item.schema_fingerprint:
            return False
    return True
