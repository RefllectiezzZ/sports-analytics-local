"""Integration tests for root entry-point scripts."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from sports_analytics.core.logging import reset_logging
from tests.helpers import repository_root, scrubbed_subprocess_environ

ENTRY_POINTS = (
    ("app", "app.py", "Streamlit application"),
    ("scraper", "scraper.py", "Data ingestion coordinator"),
    ("engine", "engine.py", "Analytics engine"),
    ("worker", "worker.py", "Background worker"),
    ("run_local", "run_local.py", "Local startup coordinator"),
)

EXPECTED_SOURCE_LINE = (
    "football-data-co-uk\tFootball-Data.co.uk\thistorical-data\t"
    "football-data-co-uk-adapter-v1\t"
    "historical-odds,historical-results,historical-statistics\tfootball"
)


@pytest.mark.parametrize(("module_name", "script", "snippet"), ENTRY_POINTS)
def test_entry_point_imports_and_main_callable(module_name: str, script: str, snippet: str) -> None:
    module = importlib.import_module(module_name)
    assert callable(module.main)
    assert (repository_root() / script).is_file()
    assert snippet


@pytest.mark.parametrize(("module_name", "script", "_snippet"), ENTRY_POINTS)
def test_validate_config_succeeds_without_side_effects(
    module_name: str,
    script: str,
    _snippet: str,
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    config = isolated_cwd / "settings.toml"
    config.write_text(
        '[application]\nenvironment = "test"\n'
        '[logging]\nfile_enabled = true\nfile_name = "sports-analytics.log"\n',
        encoding="utf-8",
    )
    module = importlib.import_module(module_name)
    code = module.main(["--config", str(config.resolve()), "--validate-config"])
    assert code == 0
    assert not (isolated_cwd / "storage").exists()
    assert not list(isolated_cwd.glob("**/*.log"))


@pytest.mark.parametrize(("module_name", "script", "_snippet"), ENTRY_POINTS)
def test_missing_config_returns_two(
    module_name: str,
    script: str,
    _snippet: str,
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    module = importlib.import_module(module_name)
    code = module.main(
        ["--config", str((isolated_cwd / "missing.toml").resolve()), "--validate-config"]
    )
    assert code == 2


@pytest.mark.parametrize(("module_name", "script", "_snippet"), ENTRY_POINTS)
def test_invalid_configuration_returns_two(
    module_name: str,
    script: str,
    _snippet: str,
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    config = isolated_cwd / "bad.toml"
    config.write_text('[logging]\nlevel = "NOPE"\n', encoding="utf-8")
    module = importlib.import_module(module_name)
    code = module.main(["--config", str(config.resolve()), "--validate-config"])
    assert code == 2


@pytest.mark.parametrize(("module_name", "script", "_snippet"), ENTRY_POINTS)
def test_normal_placeholder_execution(
    module_name: str,
    script: str,
    _snippet: str,
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = importlib.import_module(module_name)
    if module_name == "worker":
        argv = ["--once"]
    elif module_name == "run_local":
        argv = ["--worker-once"]
    elif module_name == "scraper":
        argv = ["--list-sources"]
    elif module_name == "engine":
        with pytest.raises(SystemExit) as excinfo:
            module.main([])
        assert excinfo.value.code == 2
        captured = capsys.readouterr()
        assert "build-football-1x2-features" in captured.err or "engine mode" in captured.err
        return
    else:
        argv = []
    code = module.main(argv)
    assert code == 0
    captured = capsys.readouterr()
    if module_name == "app":
        assert "streamlit interface ready" in captured.out.lower()
    elif module_name == "scraper":
        source_lines = captured.out.splitlines()
        assert source_lines == [EXPECTED_SOURCE_LINE]
        assert len(source_lines[0].split("\t")) == 6
        assert "betclic" not in captured.out.lower()
        assert "betano" not in captured.out.lower()
        assert not (isolated_cwd / "storage").exists()
        return
    elif module_name == "worker":
        assert "worker stopped:" in captured.out
        assert "stop_reason=once_no_job" in captured.out
    assert (isolated_cwd / "storage").is_dir()
    assert (isolated_cwd / "storage" / "operational.sqlite3").is_file()
    reset_logging()


def test_app_does_not_import_streamlit_as_side_effect() -> None:
    module = importlib.import_module("app")
    source = (repository_root() / "app.py").read_text(encoding="utf-8")
    assert "import streamlit" not in source
    assert "from streamlit" not in source
    assert module.main.__name__ == "main"


def test_subprocess_validate_config_exit_code(isolated_base: Path) -> None:
    repo = repository_root()
    script = repo / "engine.py"
    config = isolated_base / "settings.toml"
    config.write_text('[application]\nenvironment = "test"\n', encoding="utf-8")
    repo_log = repo / "storage" / "logs" / "sports-analytics.log"
    existed_before = repo_log.exists()
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--config",
            str(config.resolve()),
            "--validate-config",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=isolated_base,
        env=scrubbed_subprocess_environ(),
    )
    assert completed.returncode == 0
    assert "configuration valid" in completed.stdout
    assert not (isolated_base / "storage").exists()
    assert not list(isolated_base.glob("**/*.log"))
    if not existed_before:
        assert not repo_log.exists()


def test_subprocess_missing_config_exit_code(isolated_base: Path) -> None:
    repo = repository_root()
    script = repo / "worker.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--config",
            str((isolated_base / "nope.toml").resolve()),
            "--validate-config",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=isolated_base,
        env=scrubbed_subprocess_environ(),
    )
    assert completed.returncode == 2
    assert "error:" in completed.stderr.lower()
    assert "Traceback" not in completed.stderr
    assert not (isolated_base / "storage").exists()
