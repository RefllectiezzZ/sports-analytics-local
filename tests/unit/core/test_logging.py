"""Unit tests for logging configuration."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from sports_analytics.core.logging import (
    HANDLER_MARKER,
    LOGGER_NAMESPACE,
    UTCFormatter,
    configure_logging,
    reset_logging,
)
from sports_analytics.core.settings import LoggingSettings


def teardown_function() -> None:
    reset_logging()


def test_console_logging_configured(tmp_path: Path) -> None:
    settings = LoggingSettings(file_enabled=False)
    logger = configure_logging(settings, logs_directory=tmp_path)
    assert logger.name == LOGGER_NAMESPACE
    assert any(isinstance(handler, logging.StreamHandler) for handler in logger.handlers)
    assert logger.propagate is False


def test_configured_level_respected(tmp_path: Path) -> None:
    settings = LoggingSettings(level="ERROR", file_enabled=False)
    logger = configure_logging(settings, logs_directory=tmp_path)
    assert logger.level == logging.ERROR


def test_file_logging_creates_file_when_enabled(tmp_path: Path) -> None:
    settings = LoggingSettings(file_enabled=True, file_name="app.log")
    logger = configure_logging(settings, logs_directory=tmp_path)
    logger.info("hello-file")
    for handler in logger.handlers:
        handler.flush()
    log_file = tmp_path / "app.log"
    assert log_file.is_file()
    assert "hello-file" in log_file.read_text(encoding="utf-8")
    reset_logging()


def test_file_logging_inside_logs_directory(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    settings = LoggingSettings(file_enabled=True, file_name="sports-analytics.log")
    logger = configure_logging(settings, logs_directory=logs_dir)
    logger.info("probe")
    for handler in logger.handlers:
        handler.flush()
    assert (logs_dir / "sports-analytics.log").is_file()
    reset_logging()


def test_no_log_file_when_disabled(tmp_path: Path) -> None:
    settings = LoggingSettings(file_enabled=False, file_name="should-not-exist.log")
    logger = configure_logging(settings, logs_directory=tmp_path)
    logger.info("console-only")
    assert not (tmp_path / "should-not-exist.log").exists()


def test_repeated_configuration_does_not_duplicate_handlers(tmp_path: Path) -> None:
    settings = LoggingSettings(file_enabled=False)
    configure_logging(settings, logs_directory=tmp_path)
    configure_logging(settings, logs_directory=tmp_path)
    logger = logging.getLogger(LOGGER_NAMESPACE)
    managed = [handler for handler in logger.handlers if getattr(handler, HANDLER_MARKER, False)]
    assert len(managed) == 1


def test_repeated_log_calls_not_duplicated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = LoggingSettings(file_enabled=False, level="INFO")
    configure_logging(settings, logs_directory=tmp_path)
    configure_logging(settings, logs_directory=tmp_path)
    logger = logging.getLogger(LOGGER_NAMESPACE)
    logger.info("once-only")
    captured = capsys.readouterr()
    assert captured.err.count("once-only") == 1


def test_utc_formatter_configured(tmp_path: Path) -> None:
    settings = LoggingSettings(file_enabled=False)
    logger = configure_logging(settings, logs_directory=tmp_path)
    managed = [handler for handler in logger.handlers if getattr(handler, HANDLER_MARKER, False)]
    assert managed
    assert all(isinstance(handler.formatter, UTCFormatter) for handler in managed)


def test_reconfiguration_closes_managed_handlers(tmp_path: Path) -> None:
    settings = LoggingSettings(file_enabled=True, file_name="rotate.log")
    first = configure_logging(settings, logs_directory=tmp_path)
    first_managed = [
        handler for handler in first.handlers if getattr(handler, HANDLER_MARKER, False)
    ]
    assert first_managed
    configure_logging(settings, logs_directory=tmp_path)
    current = logging.getLogger(LOGGER_NAMESPACE).handlers
    for handler in first_managed:
        assert handler not in current
    reset_logging()


def test_reset_logging_releases_file_handles(tmp_path: Path) -> None:
    settings = LoggingSettings(file_enabled=True, file_name="handle.log")
    configure_logging(settings, logs_directory=tmp_path)
    logging.getLogger(LOGGER_NAMESPACE).info("written")
    reset_logging()
    log_file = tmp_path / "handle.log"
    log_file.unlink()
    assert not log_file.exists()
