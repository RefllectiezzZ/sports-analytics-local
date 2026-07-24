"""Shared test helpers for configuration and entry-point isolation."""

from __future__ import annotations

import os
from pathlib import Path

from sports_analytics.core.settings import ENV_PREFIX


def scrubbed_subprocess_environ() -> dict[str, str]:
    """Copy ``os.environ`` without ``SPORTS_ANALYTICS_*`` overrides."""
    return {key: value for key, value in os.environ.items() if not key.startswith(ENV_PREFIX)}


def repository_root() -> Path:
    """Return the repository root containing the five entry-point scripts."""
    return Path(__file__).resolve().parents[1]
