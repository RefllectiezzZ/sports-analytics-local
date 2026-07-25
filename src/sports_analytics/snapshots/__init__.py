"""Sport-agnostic immutable Parquet snapshot preparation, publication, and verification.

This package must never import a sport-specific package: every domain fact
arrives through :class:`sports_analytics.snapshots.spec.SnapshotSpec`.
"""

from sports_analytics.snapshots.spec import (
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
    DatasetDescriptor,
    SnapshotDatasetSuite,
    SnapshotHttpMetadata,
    SnapshotIdentity,
    SnapshotMetrics,
    SnapshotSpec,
)
from sports_analytics.snapshots.types import PublishedSnapshot

__all__ = [
    "MANIFEST_FILENAME",
    "MANIFEST_VERSION",
    "DatasetDescriptor",
    "PublishedSnapshot",
    "SnapshotDatasetSuite",
    "SnapshotHttpMetadata",
    "SnapshotIdentity",
    "SnapshotMetrics",
    "SnapshotSpec",
]
