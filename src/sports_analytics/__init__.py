"""Local sports analytics and betting-support package."""

from typing import Final

from sports_analytics.core import (
    ConfigurationError,
    RuntimeContext,
    Settings,
    bootstrap_runtime,
    load_settings,
)

__version__: Final[str] = "0.1.0"

__all__ = [
    "ConfigurationError",
    "RuntimeContext",
    "Settings",
    "__version__",
    "bootstrap_runtime",
    "load_settings",
]
