"""Project-specific exceptions for sports-analytics-local."""


class SportsAnalyticsError(Exception):
    """Base exception for all sports-analytics-local errors."""


class ConfigurationError(SportsAnalyticsError):
    """Raised when configuration loading or validation fails."""


class RuntimeBootstrapError(SportsAnalyticsError):
    """Raised when runtime bootstrap encounters an expected failure."""


class DatabaseError(SportsAnalyticsError):
    """Base exception for SQLite persistence failures."""


class DatabaseConnectionError(DatabaseError):
    """Raised when a SQLite connection cannot be opened or configured."""


class DatabaseMigrationError(DatabaseError):
    """Raised when migration discovery, verification, or application fails."""


class DatabaseIntegrityError(DatabaseError):
    """Raised when a database integrity or constraint rule is violated."""


class RepositoryError(DatabaseError):
    """Raised when a repository operation fails unexpectedly."""


class WorkerError(SportsAnalyticsError):
    """Raised for durable worker lifecycle or execution failures."""


class WorkerShutdownError(WorkerError):
    """Raised when a handler should stop because the worker is shutting down."""


class JobLeaseError(WorkerError, DatabaseIntegrityError):
    """Raised when lease ownership, expiry, or fencing checks fail."""


class JobHandlerError(WorkerError):
    """Raised for job-handler registration or execution failures."""


class JobRegistryError(JobHandlerError):
    """Raised when the static handler registry is misused."""


class RetryableJobError(JobHandlerError):
    """Raised by a handler to request a retry when attempts remain."""


class PermanentJobError(JobHandlerError):
    """Raised by a handler to request terminal failure without retry."""


class SourceError(SportsAnalyticsError):
    """Base exception for external source adapter failures."""


class RetryableSourceError(SourceError):
    """Raised for temporary source retrieval failures that may succeed on retry."""


class PermanentSourceError(SourceError):
    """Raised for permanent source retrieval or policy violations."""


class SourceNotFoundError(PermanentSourceError):
    """Raised when the external source reports that a resource does not exist."""


class ParserError(SportsAnalyticsError):
    """Raised when source CSV decoding or structural parsing fails permanently."""


class NormalizationError(SportsAnalyticsError):
    """Raised when canonical normalization rejects source row content."""


class SourceIntegrityError(NormalizationError):
    """Raised when source identities conflict and cannot be safely reconciled."""


class SnapshotError(SportsAnalyticsError):
    """Base exception for snapshot preparation, publication, or verification."""


class SnapshotBusyError(SnapshotError):
    """Raised when an active BUILDING snapshot blocks publication (retryable)."""


class SnapshotIntegrityError(SnapshotError):
    """Raised for permanent snapshot filesystem or metadata integrity conflicts."""


class SnapshotVerificationError(SnapshotError):
    """Raised when a READY snapshot fails read-only integrity verification."""


class FeatureError(SportsAnalyticsError):
    """Raised when feature engineering or feature-artifact handling fails."""


class ModelError(SportsAnalyticsError):
    """Raised when model training, calibration, or artifact handling fails."""


class EvaluationError(SportsAnalyticsError):
    """Raised when temporal validation or metric computation fails."""


class TrainingError(SportsAnalyticsError):
    """Raised when the training service rejects inputs or cannot produce artifacts."""
