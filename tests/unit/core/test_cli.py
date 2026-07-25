"""Unit tests for the shared CLI foundation."""

from __future__ import annotations

from pathlib import Path

import pytest

from sports_analytics.core.cli import CONFIG_ERROR_EXIT, SUCCESS_EXIT, run_component
from sports_analytics.core.logging import reset_logging


def test_validate_config_success(
    isolated_cwd: Path, clear_sports_analytics_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    config = isolated_cwd / "settings.toml"
    config.write_text('[application]\nenvironment = "test"\n', encoding="utf-8")
    code = run_component(
        "engine",
        "test",
        argv=["--config", str(config), "--validate-config"],
    )
    assert code == SUCCESS_EXIT
    captured = capsys.readouterr()
    assert "configuration valid" in captured.out
    assert not (isolated_cwd / "storage").exists()


def test_missing_config_returns_exit_code_2(
    isolated_cwd: Path, clear_sports_analytics_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = isolated_cwd / "absent.toml"
    code = run_component(
        "engine",
        "test",
        argv=["--config", str(missing), "--validate-config"],
    )
    assert code == CONFIG_ERROR_EXIT
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "Traceback" not in captured.err


def test_invalid_configuration_returns_exit_code_2(
    isolated_cwd: Path, clear_sports_analytics_env: None
) -> None:
    config = isolated_cwd / "bad.toml"
    config.write_text('[application]\nenvironment = "nope"\n', encoding="utf-8")
    code = run_component(
        "worker",
        "test",
        argv=["--config", str(config), "--validate-config"],
    )
    assert code == CONFIG_ERROR_EXIT


def test_normal_placeholder_execution(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = run_component("app", "test", argv=[])
    assert code == SUCCESS_EXIT
    captured = capsys.readouterr()
    assert "not implemented" in captured.out.lower()
    reset_logging()


def test_invalid_utf8_toml_validate_config_exit_code_2(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = isolated_cwd / "bad.toml"
    config.write_bytes(b"\xff\xfe[application]\n")
    code = run_component(
        "engine",
        "test",
        argv=["--config", str(config), "--validate-config"],
    )
    assert code == CONFIG_ERROR_EXIT
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "Traceback" not in captured.err
    assert not (isolated_cwd / "storage").exists()


def test_invalid_utf8_dotenv_validate_config_exit_code_2(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file = isolated_cwd / "bad.env"
    env_file.write_bytes(b"SPORTS_ANALYTICS_APPLICATION__NAME=\xff\xfe\n")
    code = run_component(
        "engine",
        "test",
        argv=["--env-file", str(env_file), "--validate-config"],
    )
    assert code == CONFIG_ERROR_EXIT
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "Traceback" not in captured.err
    assert not (isolated_cwd / "storage").exists()


def test_invalid_logging_format_validate_config_exit_code_2(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = isolated_cwd / "bad.toml"
    config.write_text('[logging]\nformat = "%(asctime"\n', encoding="utf-8")
    code = run_component(
        "engine",
        "test",
        argv=["--config", str(config), "--validate-config"],
    )
    assert code == CONFIG_ERROR_EXIT
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "logging.format" in captured.err
    assert "Traceback" not in captured.err
    assert not (isolated_cwd / "storage").exists()
    assert not list(isolated_cwd.glob("**/*.log"))


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_non_finite_worker_timing_validate_config_exit_code_2(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    config = isolated_cwd / "bad.toml"
    config.write_text(
        f"[worker]\npoll_interval_seconds = {value}\n",
        encoding="utf-8",
    )
    code = run_component(
        "worker",
        "test",
        argv=["--config", str(config), "--validate-config"],
    )
    assert code == CONFIG_ERROR_EXIT
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "positive finite numbers" in captured.err
    assert "Traceback" not in captured.err
    assert not (isolated_cwd / "storage").exists()


def test_invalid_component_name_validate_config(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = run_component(
        "bad component!",
        "test",
        argv=["--validate-config"],
    )
    assert code == CONFIG_ERROR_EXIT
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "component name" in captured.err
    assert "Traceback" not in captured.err
    assert not (isolated_cwd / "storage").exists()


def test_invalid_component_name_normal_mode(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = run_component("!!!", "test", argv=[])
    assert code == CONFIG_ERROR_EXIT
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert "component name" in captured.err
    assert "Traceback" not in captured.err
    assert not (isolated_cwd / "storage").exists()
