"""Typed configuration models and deterministic settings loading."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any, Final, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from sports_analytics.core.exceptions import ConfigurationError

ENV_PREFIX: Final[str] = "SPORTS_ANALYTICS_"
NESTED_DELIMITER: Final[str] = "__"
CONFIG_PATH_ENV_VAR: Final[str] = "SPORTS_ANALYTICS_CONFIG_PATH"
DEFAULT_CONFIG_PATH: Final[Path] = Path("config/settings.toml")
DEFAULT_ENV_FILE: Final[Path] = Path(".env")
MAX_DETERMINISTIC_SEED: Final[int] = 4_294_967_295
LOG_LEVELS: Final[frozenset[str]] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_SAFE_LOG_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class _FrozenModel(BaseModel):
    """Immutable Pydantic model that rejects unknown fields."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_default=True,
        str_strip_whitespace=True,
    )


class ApplicationSettings(_FrozenModel):
    """Top-level application identity and determinism settings."""

    name: str = "sports-analytics-local"
    environment: Literal["development", "test", "production"] = "development"
    timezone: str = "UTC"
    deterministic_seed: int = Field(default=42, ge=0, le=MAX_DETERMINISTIC_SEED)

    @field_validator("name")
    @classmethod
    def _name_non_empty(cls, value: str) -> str:
        if not value:
            msg = "application.name must be a non-empty string"
            raise ValueError(msg)
        return value

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            msg = f"application.timezone must be a valid IANA timezone, got {value!r}"
            raise ValueError(msg) from exc
        return value


class StorageSettings(_FrozenModel):
    """Filesystem locations for local operational and analytical data."""

    root_directory: Path = Path("storage")
    sqlite_path: Path = Path("storage/operational.sqlite3")
    raw_directory: Path = Path("storage/raw")
    snapshots_directory: Path = Path("storage/snapshots")
    features_directory: Path = Path("storage/features")
    models_directory: Path = Path("storage/models")
    exports_directory: Path = Path("storage/exports")
    logs_directory: Path = Path("storage/logs")


class LoggingSettings(_FrozenModel):
    """Console and optional rotating-file logging configuration."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    format: str = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    date_format: str = "%Y-%m-%dT%H:%M:%SZ"
    file_enabled: bool = True
    file_name: str = "sports-analytics.log"
    max_bytes: int = Field(default=1_048_576, gt=0)
    backup_count: int = Field(default=5, ge=0)

    @field_validator("format", "date_format")
    @classmethod
    def _non_empty_format(cls, value: str) -> str:
        if not value:
            msg = "logging format strings must be non-empty"
            raise ValueError(msg)
        return value

    @field_validator("file_name")
    @classmethod
    def _safe_file_name(cls, value: str) -> str:
        if not value or not _SAFE_LOG_FILE_NAME.fullmatch(value):
            msg = (
                "logging.file_name must be a safe file name without directory "
                f"separators or parent traversal, got {value!r}"
            )
            raise ValueError(msg)
        if Path(value).name != value:
            msg = f"logging.file_name must not contain directory components, got {value!r}"
            raise ValueError(msg)
        return value

    @field_validator("level", mode="before")
    @classmethod
    def _normalize_level(cls, value: object) -> object:
        if isinstance(value, str):
            upper = value.upper()
            if upper not in LOG_LEVELS:
                msg = f"logging.level must be one of {sorted(LOG_LEVELS)}, got {value!r}"
                raise ValueError(msg)
            return upper
        return value


class WorkerSettings(_FrozenModel):
    """Background worker timing settings (loop not implemented)."""

    poll_interval_seconds: float = Field(default=30, gt=0)
    heartbeat_interval_seconds: float = Field(default=15, gt=0)
    stale_job_timeout_seconds: float = Field(default=300, gt=0)

    @model_validator(mode="after")
    def _stale_after_heartbeat(self) -> Self:
        if self.stale_job_timeout_seconds <= self.heartbeat_interval_seconds:
            msg = (
                "worker.stale_job_timeout_seconds must be greater than "
                "worker.heartbeat_interval_seconds"
            )
            raise ValueError(msg)
        return self


class ScrapingSettings(_FrozenModel):
    """Scraping coordinator settings (adapters not implemented)."""

    enabled: bool = False
    request_timeout_seconds: float = Field(default=30, gt=0)
    maximum_retries: int = Field(default=3, ge=0)
    browser_headless: bool = True


class ModellingSettings(_FrozenModel):
    """Local modelling settings (training not implemented)."""

    enabled: bool = False
    default_random_seed: int = Field(default=42, ge=0, le=MAX_DETERMINISTIC_SEED)
    minimum_training_samples: int = Field(default=100, gt=0)


class Settings(_FrozenModel):
    """Complete immutable application configuration."""

    application: ApplicationSettings = Field(default_factory=ApplicationSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    scraping: ScrapingSettings = Field(default_factory=ScrapingSettings)
    modelling: ModellingSettings = Field(default_factory=ModellingSettings)


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge mappings without mutating inputs.

    Nested mappings are merged. Scalars from ``override`` replace scalars from
    ``base``. A mapping colliding with a non-mapping raises ``ConfigurationError``.
    """
    result: dict[str, Any] = dict(base)
    for key in sorted(override):
        override_value = override[key]
        if key not in result:
            result[key] = _copy_mapping_value(override_value)
            continue
        base_value = result[key]
        if isinstance(base_value, Mapping) and isinstance(override_value, Mapping):
            result[key] = deep_merge(base_value, override_value)
        elif isinstance(base_value, Mapping) != isinstance(override_value, Mapping):
            msg = (
                f"configuration type conflict for key {key!r}: "
                f"cannot merge {type(base_value).__name__} with "
                f"{type(override_value).__name__}"
            )
            raise ConfigurationError(msg)
        else:
            result[key] = _copy_mapping_value(override_value)
    return result


def _copy_mapping_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _copy_mapping_value(value[key]) for key in sorted(value)}
    return value


def environ_to_nested_mapping(
    environ: Mapping[str, str],
    *,
    prefix: str = ENV_PREFIX,
) -> dict[str, Any]:
    """Convert prefixed environment variables into a nested override mapping."""
    nested: dict[str, Any] = {}
    for raw_key in sorted(environ):
        if not raw_key.startswith(prefix):
            continue
        if raw_key == CONFIG_PATH_ENV_VAR:
            continue
        remainder = raw_key[len(prefix) :]
        if not remainder:
            msg = f"unknown configuration variable {raw_key!r}: empty path after prefix"
            raise ConfigurationError(msg)
        parts = [part.lower() for part in remainder.split(NESTED_DELIMITER)]
        if any(not part for part in parts):
            msg = f"unknown configuration variable {raw_key!r}: empty nested segment"
            raise ConfigurationError(msg)
        _assign_nested(nested, parts, environ[raw_key], raw_key)
    return nested


def _assign_nested(
    target: MutableMapping[str, Any],
    parts: list[str],
    value: str,
    raw_key: str,
) -> None:
    cursor: MutableMapping[str, Any] = target
    for index, part in enumerate(parts[:-1]):
        existing = cursor.get(part)
        if existing is None:
            new_map: dict[str, Any] = {}
            cursor[part] = new_map
            cursor = new_map
            continue
        if not isinstance(existing, MutableMapping):
            path = ".".join(parts[: index + 1])
            msg = (
                f"configuration conflict for {raw_key!r}: "
                f"{path!r} is already a scalar and cannot hold nested keys"
            )
            raise ConfigurationError(msg)
        cursor = existing
    leaf = parts[-1]
    if leaf in cursor and isinstance(cursor[leaf], Mapping):
        msg = f"configuration conflict for {raw_key!r}: {'.'.join(parts)!r} is already a mapping"
        raise ConfigurationError(msg)
    cursor[leaf] = value


def _load_toml_mapping(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.is_file():
        if required:
            msg = f"configuration file not found: {path}"
            raise ConfigurationError(msg)
        return {}
    try:
        raw_text = path.read_text(encoding="utf-8")
        loaded = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        msg = f"invalid TOML in configuration file {path}: {exc}"
        raise ConfigurationError(msg) from exc
    except OSError as exc:
        msg = f"unable to read configuration file {path}: {exc}"
        raise ConfigurationError(msg) from exc
    if not isinstance(loaded, dict):
        msg = f"configuration file {path} must contain a TOML table at the root"
        raise ConfigurationError(msg)
    return loaded


def _load_dotenv_mapping(path: Path, *, required: bool) -> dict[str, str]:
    if not path.is_file():
        if required:
            msg = f"environment file not found: {path}"
            raise ConfigurationError(msg)
        return {}
    try:
        values = dotenv_values(path)
    except OSError as exc:
        msg = f"unable to read environment file {path}: {exc}"
        raise ConfigurationError(msg) from exc
    result: dict[str, str] = {}
    for key, value in values.items():
        if key is None or value is None:
            continue
        result[str(key)] = str(value)
    return result


def _resolve_config_path(
    *,
    explicit_config_path: Path | str | None,
    os_environ: Mapping[str, str],
    dotenv_mapping: Mapping[str, str],
    base_directory: Path,
) -> tuple[Path, bool]:
    """Return ``(path, required)`` for the selected TOML configuration file."""
    if explicit_config_path is not None:
        path = Path(explicit_config_path)
        if not path.is_absolute():
            path = (base_directory / path).resolve()
        else:
            path = path.resolve()
        return path, True

    if CONFIG_PATH_ENV_VAR in os_environ:
        path = Path(os_environ[CONFIG_PATH_ENV_VAR])
        if not path.is_absolute():
            path = (base_directory / path).resolve()
        else:
            path = path.resolve()
        return path, True

    if CONFIG_PATH_ENV_VAR in dotenv_mapping:
        path = Path(dotenv_mapping[CONFIG_PATH_ENV_VAR])
        if not path.is_absolute():
            path = (base_directory / path).resolve()
        else:
            path = path.resolve()
        return path, True

    default_path = base_directory / DEFAULT_CONFIG_PATH
    return default_path.resolve(), False


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ()))
        reason = error.get("msg", "invalid value")
        if location:
            parts.append(f"{location}: {reason}")
        else:
            parts.append(str(reason))
    return "; ".join(parts) if parts else str(exc)


def _defaults_as_mapping() -> dict[str, Any]:
    return Settings().model_dump(mode="python")


def load_settings(
    *,
    config_path: Path | str | None = None,
    env_file: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
    base_directory: Path | str | None = None,
) -> Settings:
    """Load and validate settings from layered configuration sources.

    Precedence (lowest to highest):

    1. built-in model defaults
    2. TOML configuration file
    3. ``.env`` file
    4. operating-system environment variables
    5. explicit programmatic overrides

    Config-file selection precedence is separate: explicit ``config_path``, then
    ``SPORTS_ANALYTICS_CONFIG_PATH`` from ``environ``, then from ``.env``, then
    ``config/settings.toml``.
    """
    base_dir = Path(base_directory) if base_directory is not None else Path.cwd()
    if not base_dir.is_absolute():
        base_dir = base_dir.resolve()
    else:
        base_dir = base_dir.resolve()

    os_environ: Mapping[str, str] = dict(os.environ) if environ is None else environ

    if env_file is None:
        env_path = (base_dir / DEFAULT_ENV_FILE).resolve()
        env_required = False
    else:
        env_path = Path(env_file)
        if not env_path.is_absolute():
            env_path = (base_dir / env_path).resolve()
        else:
            env_path = env_path.resolve()
        env_required = True

    dotenv_mapping = _load_dotenv_mapping(env_path, required=env_required)

    selected_config_path, config_required = _resolve_config_path(
        explicit_config_path=config_path,
        os_environ=os_environ,
        dotenv_mapping=dotenv_mapping,
        base_directory=base_dir,
    )
    toml_mapping = _load_toml_mapping(selected_config_path, required=config_required)

    dotenv_overrides = environ_to_nested_mapping(dotenv_mapping)
    os_overrides = environ_to_nested_mapping(os_environ)
    explicit = dict(overrides) if overrides is not None else {}

    merged = deep_merge(_defaults_as_mapping(), toml_mapping)
    merged = deep_merge(merged, dotenv_overrides)
    merged = deep_merge(merged, os_overrides)
    merged = deep_merge(merged, explicit)

    try:
        return Settings.model_validate(merged)
    except ValidationError as exc:
        msg = (
            "configuration validation failed"
            f" (config={selected_config_path}): {_format_validation_error(exc)}"
        )
        raise ConfigurationError(msg) from exc
    except ConfigurationError:
        raise
    except Exception as exc:  # noqa: BLE001 - wrap unexpected construction failures
        msg = f"configuration validation failed (config={selected_config_path}): {exc}"
        raise ConfigurationError(msg) from exc
