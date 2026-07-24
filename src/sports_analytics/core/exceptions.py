"""Project-specific exceptions for sports-analytics-local."""


class SportsAnalyticsError(Exception):
    """Base exception for all sports-analytics-local errors."""


class ConfigurationError(SportsAnalyticsError):
    """Raised when configuration loading or validation fails."""


class RuntimeBootstrapError(SportsAnalyticsError):
    """Raised when runtime bootstrap encounters an expected failure."""
