"""Reusable local runtime bootstrap and validation helpers."""

from __future__ import annotations

import random
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from logging import Logger
from pathlib import Path
from typing import Any, Final

import numpy as np

from sports_analytics.core.exceptions import (
    ConfigurationError,
    DatabaseError,
    RuntimeBootstrapError,
)
from sports_analytics.core.logging import configure_logging, get_component_logger
from sports_analytics.core.paths import RuntimePaths, create_runtime_directories, resolve_paths
from sports_analytics.core.settings import Settings, load_settings
from sports_analytics.data.service import initialize_operational_database

_COMPONENT_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Immutable context returned by a successful runtime bootstrap."""

    component: str
    settings: Settings
    paths: RuntimePaths
    started_at: datetime
    logger: Logger
    database_path: Path
    schema_version: int


def validate_component_name(component: str) -> str:
    """Validate a component identifier used for logging and CLI entry points."""
    if not component or not _COMPONENT_NAME_PATTERN.fullmatch(component):
        msg = (
            "component name must be a non-empty identifier matching "
            f"[A-Za-z][A-Za-z0-9_-]*, got {component!r}"
        )
        raise ConfigurationError(msg)
    return component


def seed_deterministic_generators(seed: int) -> None:
    """Seed Python ``random`` and NumPy's legacy global generator.

    Future application code should prefer explicitly constructed random
    generators where practical rather than relying solely on global state.
    """
    random.seed(seed)
    np.random.seed(seed)


def bootstrap_runtime(
    component: str,
    *,
    config_path: Path | str | None = None,
    env_file: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
    base_directory: Path | str | None = None,
) -> RuntimeContext:
    """Load settings, prepare local runtime directories, migrate SQLite, and configure logging."""
    component_name = validate_component_name(component)
    base_dir = Path(base_directory) if base_directory is not None else Path.cwd()

    settings = load_settings(
        config_path=config_path,
        env_file=env_file,
        environ=environ,
        overrides=overrides,
        base_directory=base_dir,
    )
    paths = resolve_paths(settings, base_dir)
    create_runtime_directories(paths)
    seed_deterministic_generators(settings.application.deterministic_seed)
    configure_logging(settings.logging, logs_directory=paths.logs_directory)
    logger = get_component_logger(component_name)

    try:
        readiness = initialize_operational_database(paths)
    except DatabaseError as exc:
        msg = f"database initialization failed for {paths.sqlite_path}: {exc}"
        raise RuntimeBootstrapError(msg) from exc

    started_at = datetime.now(tz=UTC)
    logger.info(
        "runtime bootstrap complete component=%s environment=%s timezone=%s "
        "seed=%s base_directory=%s file_logging=%s schema_version=%s database_path=%s",
        component_name,
        settings.application.environment,
        settings.application.timezone,
        settings.application.deterministic_seed,
        paths.base_directory,
        settings.logging.file_enabled,
        readiness.schema_version,
        readiness.database_path,
    )
    return RuntimeContext(
        component=component_name,
        settings=settings,
        paths=paths,
        started_at=started_at,
        logger=logger,
        database_path=readiness.database_path,
        schema_version=readiness.schema_version,
    )


def validate_configuration(
    *,
    config_path: Path | str | None = None,
    env_file: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
    base_directory: Path | str | None = None,
) -> tuple[Settings, RuntimePaths]:
    """Validate settings and resolve paths without runtime side effects.

    Does not create directories, configure persistent logging handlers, seed
    global random generators, create a SQLite file, or apply migrations.
    """
    base_dir = Path(base_directory) if base_directory is not None else Path.cwd()
    try:
        settings = load_settings(
            config_path=config_path,
            env_file=env_file,
            environ=environ,
            overrides=overrides,
            base_directory=base_dir,
        )
    except ConfigurationError:
        raise
    except Exception as exc:  # noqa: BLE001
        msg = f"configuration validation failed: {exc}"
        raise ConfigurationError(msg) from exc

    paths = resolve_paths(settings, base_dir)
    return settings, paths


def format_validation_success(settings: Settings, paths: RuntimePaths) -> str:
    """Return a concise human-readable validation success message."""
    return (
        "configuration valid: "
        f"environment={settings.application.environment} "
        f"timezone={settings.application.timezone} "
        f"base_directory={paths.base_directory} "
        f"storage_root={paths.storage_root}"
    )
