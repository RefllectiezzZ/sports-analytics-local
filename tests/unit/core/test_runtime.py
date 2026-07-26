"""Unit tests for runtime bootstrap and validation-only mode."""

from __future__ import annotations

import random
from datetime import UTC
from pathlib import Path

import numpy as np
import pytest

from sports_analytics.core.exceptions import ConfigurationError
from sports_analytics.core.logging import reset_logging
from sports_analytics.core.runtime import (
    bootstrap_runtime,
    seed_deterministic_generators,
    validate_configuration,
)


def teardown_function() -> None:
    reset_logging()


def test_bootstrap_returns_complete_context(tmp_path: Path) -> None:
    context = bootstrap_runtime(
        "engine",
        environ={},
        base_directory=tmp_path,
        overrides={"logging": {"file_enabled": False}},
    )
    assert context.component == "engine"
    assert context.settings.application.name == "sports-analytics-local"
    assert context.paths.base_directory == tmp_path.resolve()
    assert context.started_at.tzinfo is not None
    assert context.started_at.utcoffset() == UTC.utcoffset(context.started_at)
    assert context.logger.name.endswith(".engine")


def test_bootstrap_creates_directories_and_sqlite(tmp_path: Path) -> None:
    context = bootstrap_runtime(
        "worker",
        environ={},
        base_directory=tmp_path,
        overrides={"logging": {"file_enabled": False}},
    )
    assert context.paths.storage_root.is_dir()
    assert context.paths.logs_directory.is_dir()
    assert context.paths.sqlite_path.is_file()
    assert context.database_path == context.paths.sqlite_path.resolve()
    assert context.schema_version == 5


def test_bootstrap_applies_deterministic_seeding(tmp_path: Path) -> None:
    bootstrap_runtime(
        "app",
        environ={},
        base_directory=tmp_path,
        overrides={
            "application": {"deterministic_seed": 123},
            "logging": {"file_enabled": False},
        },
    )
    first = random.random()
    seed_deterministic_generators(123)
    second = random.random()
    assert first == second


def test_invalid_component_name_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="component name"):
        bootstrap_runtime(
            "",
            environ={},
            base_directory=tmp_path,
            overrides={"logging": {"file_enabled": False}},
        )


def test_validation_only_creates_no_directories_or_log_file(tmp_path: Path) -> None:
    settings, paths = validate_configuration(
        environ={},
        base_directory=tmp_path,
        overrides={"logging": {"file_enabled": True}},
    )
    assert settings.application.environment == "development"
    assert not paths.storage_root.exists()
    assert not (paths.logs_directory / settings.logging.file_name).exists()
    assert not paths.sqlite_path.exists()


def test_validation_only_does_not_alter_random_state(tmp_path: Path) -> None:
    random.seed(99)
    before = [random.random() for _ in range(3)]
    random.seed(99)
    validate_configuration(environ={}, base_directory=tmp_path)
    after = [random.random() for _ in range(3)]
    assert before == after


def test_python_random_reproducible_after_reseed() -> None:
    seed_deterministic_generators(7)
    first = [random.random() for _ in range(5)]
    seed_deterministic_generators(7)
    second = [random.random() for _ in range(5)]
    assert first == second


def test_numpy_random_reproducible_after_reseed() -> None:
    seed_deterministic_generators(11)
    first = np.random.rand(4).tolist()
    seed_deterministic_generators(11)
    second = np.random.rand(4).tolist()
    assert first == second


def test_changing_seed_changes_sequence() -> None:
    seed_deterministic_generators(1)
    first = [random.random() for _ in range(3)]
    seed_deterministic_generators(2)
    second = [random.random() for _ in range(3)]
    assert first != second


def test_bootstrap_configures_component_logger(tmp_path: Path) -> None:
    context = bootstrap_runtime(
        "scraper",
        environ={},
        base_directory=tmp_path,
        overrides={"logging": {"file_enabled": False}},
    )
    assert context.logger.name == "sports_analytics.scraper"
