"""Local logging configuration for the sports_analytics namespace."""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Final

from sports_analytics.core.exceptions import RuntimeBootstrapError
from sports_analytics.core.settings import LoggingSettings

LOGGER_NAMESPACE: Final[str] = "sports_analytics"
HANDLER_MARKER: Final[str] = "_sports_analytics_managed"


class UTCFormatter(logging.Formatter):
    """Logging formatter that always emits UTC timestamps."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """Format the record creation time in UTC."""
        created = datetime.fromtimestamp(record.created, tz=UTC)
        if datefmt:
            return created.strftime(datefmt)
        return created.isoformat(timespec="seconds").replace("+00:00", "Z")


def _mark_handler(handler: logging.Handler) -> None:
    setattr(handler, HANDLER_MARKER, True)


def _is_managed_handler(handler: logging.Handler) -> bool:
    return bool(getattr(handler, HANDLER_MARKER, False))


def reset_logging() -> None:
    """Close and remove project-managed handlers from the package logger."""
    logger = logging.getLogger(LOGGER_NAMESPACE)
    remaining: list[logging.Handler] = []
    for handler in list(logger.handlers):
        if _is_managed_handler(handler):
            handler.close()
        else:
            remaining.append(handler)
    logger.handlers = remaining


def configure_logging(
    settings: LoggingSettings,
    *,
    logs_directory: Path,
) -> logging.Logger:
    """Configure the ``sports_analytics`` logger idempotently.

    Console logging always writes to stderr. Optional rotating file logging
    writes only inside ``logs_directory`` using ``settings.file_name``.

    New handlers are fully constructed before any existing project-managed
    handlers are replaced. On failure, newly created handlers are closed and
    unrelated handlers are left untouched.
    """
    logger = logging.getLogger(LOGGER_NAMESPACE)
    logger.setLevel(settings.level)
    logger.propagate = False

    created: list[logging.Handler] = []
    try:
        try:
            formatter = UTCFormatter(fmt=settings.format, datefmt=settings.date_format)
        except ValueError as exc:
            msg = f"failed to configure logging format: {exc}"
            raise RuntimeBootstrapError(msg) from exc

        console = logging.StreamHandler(stream=sys.stderr)
        console.setLevel(settings.level)
        console.setFormatter(formatter)
        _mark_handler(console)
        created.append(console)

        if settings.file_enabled:
            log_path = logs_directory / settings.file_name
            try:
                logs_directory.mkdir(parents=True, exist_ok=True)
                file_handler = RotatingFileHandler(
                    log_path,
                    maxBytes=settings.max_bytes,
                    backupCount=settings.backup_count,
                    encoding="utf-8",
                )
            except OSError as exc:
                msg = f"failed to configure log file at {log_path}: {exc}"
                raise RuntimeBootstrapError(msg) from exc
            file_handler.setLevel(settings.level)
            file_handler.setFormatter(formatter)
            _mark_handler(file_handler)
            created.append(file_handler)
    except Exception:
        for handler in created:
            handler.close()
        raise

    reset_logging()
    for handler in created:
        logger.addHandler(handler)
    return logger


def get_component_logger(component: str) -> logging.Logger:
    """Return a child logger under the sports_analytics namespace."""
    return logging.getLogger(f"{LOGGER_NAMESPACE}.{component}")
