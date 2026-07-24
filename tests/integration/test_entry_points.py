"""Integration tests for root entry-point scripts."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from sports_analytics.core.logging import reset_logging

ENTRY_POINTS = (
    ("app", "app.py", "Streamlit application"),
    ("scraper", "scraper.py", "Data ingestion coordinator"),
    ("engine", "engine.py", "Analytics engine"),
    ("worker", "worker.py", "Background worker"),
    ("run_local", "run_local.py", "Local startup coordinator"),
)


def teardown_function() -> None:
    reset_logging()


@pytest.mark.parametrize(("module_name", "script", "snippet"), ENTRY_POINTS)
def test_entry_point_imports_and_main_callable(module_name: str, script: str, snippet: str) -> None:
    module = importlib.import_module(module_name)
    assert callable(module.main)
    assert Path(script).is_file()
    assert snippet


@pytest.mark.parametrize(("module_name", "script", "_snippet"), ENTRY_POINTS)
def test_validate_config_succeeds_without_side_effects(
    module_name: str,
    script: str,
    _snippet: str,
    tmp_path: Path,
) -> None:
    config = tmp_path / "settings.toml"
    config.write_text(
        '[application]\nenvironment = "test"\n'
        '[logging]\nfile_enabled = true\nfile_name = "sports-analytics.log"\n',
        encoding="utf-8",
    )
    module = importlib.import_module(module_name)
    code = module.main(["--config", str(config), "--validate-config"])
    assert code == 0
    assert not (tmp_path / "storage").exists()
    assert not list(tmp_path.glob("**/*.log"))


@pytest.mark.parametrize(("module_name", "script", "_snippet"), ENTRY_POINTS)
def test_missing_config_returns_two(
    module_name: str, script: str, _snippet: str, tmp_path: Path
) -> None:
    module = importlib.import_module(module_name)
    code = module.main(["--config", str(tmp_path / "missing.toml"), "--validate-config"])
    assert code == 2


@pytest.mark.parametrize(("module_name", "script", "_snippet"), ENTRY_POINTS)
def test_invalid_configuration_returns_two(
    module_name: str, script: str, _snippet: str, tmp_path: Path
) -> None:
    config = tmp_path / "bad.toml"
    config.write_text('[logging]\nlevel = "NOPE"\n', encoding="utf-8")
    module = importlib.import_module(module_name)
    code = module.main(["--config", str(config), "--validate-config"])
    assert code == 2


@pytest.mark.parametrize(("module_name", "script", "_snippet"), ENTRY_POINTS)
def test_normal_placeholder_execution(
    module_name: str,
    script: str,
    _snippet: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    module = importlib.import_module(module_name)
    code = module.main([])
    assert code == 0
    captured = capsys.readouterr()
    assert "not implemented" in captured.out.lower()
    assert (tmp_path / "storage").is_dir()
    assert not (tmp_path / "storage" / "operational.sqlite3").exists()
    reset_logging()


def test_app_does_not_import_streamlit_as_side_effect() -> None:
    module = importlib.import_module("app")
    source = Path("app.py").read_text(encoding="utf-8")
    assert "import streamlit" not in source
    assert "from streamlit" not in source
    assert module.main.__name__ == "main"


def test_subprocess_validate_config_exit_code(tmp_path: Path) -> None:
    config = tmp_path / "settings.toml"
    config.write_text('[application]\nenvironment = "test"\n', encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "engine.py", "--config", str(config), "--validate-config"],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert completed.returncode == 0
    assert "configuration valid" in completed.stdout


def test_subprocess_missing_config_exit_code(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "worker.py",
            "--config",
            str(tmp_path / "nope.toml"),
            "--validate-config",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert completed.returncode == 2
    assert "error:" in completed.stderr.lower()
