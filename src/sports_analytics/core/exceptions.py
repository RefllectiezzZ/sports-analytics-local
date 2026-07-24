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
