"""Integration tests for database CLI modes across root entry points."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from sports_analytics.core.cli import CONFIG_ERROR_EXIT, SUCCESS_EXIT, run_component
from sports_analytics.core.logging import reset_logging
from tests.helpers import repository_root, scrubbed_subprocess_environ

ENTRY_POINTS = ("app", "scraper", "engine", "worker", "run_local")


def _write_config(base: Path) -> Path:
    config = base / "settings.toml"
    config.write_text(
        '[application]\nenvironment = "test"\n'
        "[logging]\nfile_enabled = false\n"
        '[storage]\nsqlite_path = "db/ops.sqlite3"\n',
        encoding="utf-8",
    )
    return config


@pytest.mark.parametrize("component", ENTRY_POINTS)
def test_database_cli_modes(
    component: str,
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _write_config(isolated_cwd)
    module = importlib.import_module(component)
    db_path = isolated_cwd / "db" / "ops.sqlite3"

    code = module.main(["--config", str(config), "--database-status"])
    assert code == CONFIG_ERROR_EXIT
    err = capsys.readouterr().err
    assert "error:" in err
    assert "Traceback" not in err
    assert not db_path.exists()

    code = module.main(["--config", str(config), "--migrate-database"])
    assert code == SUCCESS_EXIT
    out = capsys.readouterr().out
    assert "database migrated" in out
    assert db_path.is_file()

    code = module.main(["--config", str(config), "--database-status"])
    assert code == SUCCESS_EXIT
    out = capsys.readouterr().out
    assert "database valid" in out
    assert "current_version=3" in out

    code = module.main(["--config", str(config), "--migrate-database"])
    assert code == SUCCESS_EXIT
    out = capsys.readouterr().out
    assert "previous_version=3" in out
    assert "migrations_applied=(none)" in out


def test_mutually_exclusive_modes_rejected(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    config = _write_config(isolated_cwd)
    with pytest.raises(SystemExit):
        run_component(
            "engine",
            "test",
            argv=["--config", str(config), "--validate-config", "--database-status"],
        )


def test_normal_execution_migrates_automatically(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _write_config(isolated_cwd)
    code = run_component("app", "test", argv=["--config", str(config)])
    assert code == SUCCESS_EXIT
    assert "streamlit interface ready" in capsys.readouterr().out.lower()
    assert (isolated_cwd / "db" / "ops.sqlite3").is_file()
    reset_logging()


def test_validate_config_remains_side_effect_free(
    isolated_cwd: Path,
    clear_sports_analytics_env: None,
) -> None:
    config = _write_config(isolated_cwd)
    code = run_component(
        "engine",
        "test",
        argv=["--config", str(config), "--validate-config"],
    )
    assert code == SUCCESS_EXIT
    assert not (isolated_cwd / "db").exists()
    assert not (isolated_cwd / "storage").exists()


def test_subprocess_database_cli_flow(isolated_base: Path) -> None:
    repo = repository_root()
    script = repo / "engine.py"
    config = _write_config(isolated_base)
    db_path = isolated_base / "db" / "ops.sqlite3"
    env = scrubbed_subprocess_environ()

    missing = subprocess.run(
        [sys.executable, str(script), "--config", str(config.resolve()), "--database-status"],
        check=False,
        capture_output=True,
        text=True,
        cwd=isolated_base,
        env=env,
    )
    assert missing.returncode == 2
    assert "error:" in missing.stderr.lower()
    assert "Traceback" not in missing.stderr
    assert not db_path.exists()

    migrate = subprocess.run(
        [sys.executable, str(script), "--config", str(config.resolve()), "--migrate-database"],
        check=False,
        capture_output=True,
        text=True,
        cwd=isolated_base,
        env=env,
    )
    assert migrate.returncode == 0
    assert "database migrated" in migrate.stdout
    assert db_path.is_file()

    status = subprocess.run(
        [sys.executable, str(script), "--config", str(config.resolve()), "--database-status"],
        check=False,
        capture_output=True,
        text=True,
        cwd=isolated_base,
        env=env,
    )
    assert status.returncode == 0
    assert "database valid" in status.stdout
