"""Generic snapshot publication result types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sports_analytics.core.exceptions import SnapshotIntegrityError
from sports_analytics.data.types import JsonValue, SnapshotStatus
from sports_analytics.snapshots.spec import SnapshotMetrics


@dataclass(frozen=True, slots=True)
class PublishedSnapshot:
    """Result of publishing, recovering, adopting, or reusing an immutable snapshot.

    The result is deliberately domain-neutral: dataset counts are exposed through
    generic ``metrics`` and partition values through ``partition_keys`` so no
    sport-specific field name leaks into shared infrastructure.
    """

    snapshot_id: str
    snapshot_status: SnapshotStatus
    snapshot_reused: bool
    snapshot_relative_path: str
    snapshot_type: str
    schema_version: str
    source_name: str
    source_version: str
    raw_artifact_sha256: str
    manifest_checksum_sha256: str
    partition_keys: tuple[tuple[str, str], ...]
    metrics: SnapshotMetrics
    domain_metadata: dict[str, JsonValue]
    source_observed_at_utc: datetime

    def row_count(self, dataset_name: str) -> int:
        """Return the published row count for one dataset."""
        return self.metrics.row_count(dataset_name)

    def partition_value(self, key: str) -> str:
        """Return one partition value by key."""
        for partition_key, value in self.partition_keys:
            if partition_key == key:
                return value
        msg = f"unknown snapshot partition key: {key}"
        raise SnapshotIntegrityError(msg)
