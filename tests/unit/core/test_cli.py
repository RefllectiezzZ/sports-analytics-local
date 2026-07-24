"""Unit tests for the shared CLI foundation."""

from __future__ import annotations

from pathlib import Path

from sports_analytics.core.cli import CONFIG_ERROR_EXIT, SUCCESS_EXIT, run_component
from sports_analytics.core.logging import reset_logging


def teardown_function() -> None:
    reset_logging()


def test_validate_config_success(tmp_path: Path, capsys: object) -> None:
    config = tmp_path / "settings.toml"
    config.write_text('[application]\nenvironment = "test"\n', encoding="utf-8")
    code = run_component(
        "engine",
        "test",
        argv=["--config", str(config), "--validate-config"],
    )
    assert code == SUCCESS_EXIT
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "configuration valid" in captured.out
    assert not (tmp_path / "storage").exists()


def test_missing_config_returns_exit_code_2(tmp_path: Path, capsys: object) -> None:
    missing = tmp_path / "absent.toml"
    code = run_component(
        "engine",
        "test",
        argv=["--config", str(missing), "--validate-config"],
    )
    assert code == CONFIG_ERROR_EXIT
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "error:" in captured.err


def test_invalid_configuration_returns_exit_code_2(tmp_path: Path) -> None:
    config = tmp_path / "bad.toml"
    config.write_text('[application]\nenvironment = "nope"\n', encoding="utf-8")
    code = run_component(
        "worker",
        "test",
        argv=["--config", str(config), "--validate-config"],
    )
    assert code == CONFIG_ERROR_EXIT


def test_normal_placeholder_execution(tmp_path: Path, monkeypatch: object, capsys: object) -> None:
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    code = run_component("engine", "test", argv=[])
    assert code == SUCCESS_EXIT
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "not implemented" in captured.out.lower()
    reset_logging()
