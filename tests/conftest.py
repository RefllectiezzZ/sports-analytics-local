"""Shared pytest fixtures for configuration and entry-point isolation."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from sports_analytics.core.logging import reset_logging
from sports_analytics.core.settings import ENV_PREFIX


@pytest.fixture
def clear_sports_analytics_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove inherited ``SPORTS_ANALYTICS_*`` variables from the process env."""
    for key in list(os.environ):
        if key.startswith(ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def isolated_base(
    tmp_path: Path,
    clear_sports_analytics_env: None,
) -> Path:
    """Temporary base directory with no default ``.env`` or settings.toml."""
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "config" / "settings.toml").exists()
    return tmp_path


@pytest.fixture
def isolated_cwd(
    isolated_base: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Change cwd to an isolated temporary base for entry-point tests."""
    monkeypatch.chdir(isolated_base)
    return isolated_base


@pytest.fixture(autouse=True)
def _reset_project_logging() -> Iterator[None]:
    """Ensure project-managed logging handlers do not leak across tests."""
    yield
    reset_logging()
