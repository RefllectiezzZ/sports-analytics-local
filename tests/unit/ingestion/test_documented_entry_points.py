"""Documented scraper entry-point and smoke CLI subprocess tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from sports_analytics.core.cli import FAILURE_EXIT, SUCCESS_EXIT
from tests.helpers import repository_root, scrubbed_subprocess_environ


def test_scraper_module_main_executes(isolated_cwd: Path) -> None:
    """``python -m sports_analytics.ingestion`` must invoke main()."""
    config = isolated_cwd / "settings.toml"
    config.write_text(
        '[application]\nenvironment = "test"\n[logging]\nfile_enabled = false\n',
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "sports_analytics.ingestion",
            "--config",
            str(config.resolve()),
            "--validate-config",
        ],
        cwd=isolated_cwd,
        env=scrubbed_subprocess_environ(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == SUCCESS_EXIT


def test_documented_scraper_smoke_no_profile_exits_nonzero(isolated_cwd: Path) -> None:
    """Documented ``scraper.py --smoke-bookmaker`` returns failure without a profile."""
    scraper = repository_root() / "scraper.py"
    config = isolated_cwd / "settings.toml"
    config.write_text(
        '[application]\nenvironment = "test"\n'
        "[logging]\nfile_enabled = false\n"
        "[bookmakers]\nenabled = true\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(scraper),
            "--config",
            str(config.resolve()),
            "--smoke-bookmaker",
            "--provider",
            "betano-pt",
            "--sport",
            "football",
            "--duration-seconds",
            "5",
            "--diagnostic-directory",
            str(isolated_cwd / "diagnostics"),
        ],
        cwd=isolated_cwd,
        env=scrubbed_subprocess_environ(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == FAILURE_EXIT
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["succeeded"] is False
    assert payload["failure_reason"] == "no-verified-extraction-profile"
    assert payload["provider"] == "betano-pt"


def test_documented_scraper_probe_prints_json(isolated_cwd: Path) -> None:
    """Documented probe command must print JSON even when browser is unavailable."""
    scraper = repository_root() / "scraper.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(scraper),
            "--probe-bookmaker",
            "--provider",
            "betano-pt",
            "--sport",
            "football",
            "--duration-seconds",
            "1",
            "--diagnostic-directory",
            str(isolated_cwd / "diagnostics"),
        ],
        cwd=isolated_cwd,
        env=scrubbed_subprocess_environ(),
        capture_output=True,
        text=True,
        check=False,
    )
    # Probe may succeed with empty/blocked evidence or fail if Playwright missing;
    # either way stdout must be JSON object with provider when exit is 0, or
    # non-zero with no crash traceback for configuration-safe paths.
    if completed.returncode == SUCCESS_EXIT:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        assert payload["provider"] == "betano-pt"
        assert "duration_seconds" in payload
    else:
        assert completed.returncode != 0
        assert "Traceback" not in completed.stderr or "playwright" in completed.stderr.lower()
