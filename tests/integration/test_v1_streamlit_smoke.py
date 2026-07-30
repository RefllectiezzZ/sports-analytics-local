"""Bounded localhost-only smoke test for the package-native Streamlit UI."""

from __future__ import annotations

import http.client
import socket
import subprocess
import sys
import time
from pathlib import Path

from sports_analytics.release.cli import initialize_v1


def test_package_streamlit_entry_health_and_shutdown(tmp_path: Path) -> None:
    config = tmp_path / "settings.toml"
    config.write_text(
        '[application]\nenvironment = "test"\n'
        "[logging]\nfile_enabled = false\n"
        '[storage]\nroot_directory = "runtime"\n'
        'sqlite_path = "runtime/operational.sqlite3"\n'
        'raw_directory = "runtime/raw"\n'
        'snapshots_directory = "runtime/snapshots"\n'
        'features_directory = "runtime/features"\n'
        'models_directory = "runtime/models"\n'
        'exports_directory = "runtime/exports"\n'
        'logs_directory = "runtime/logs"\n',
        encoding="utf-8",
    )
    initialize_v1(config_path=config, base_directory=tmp_path)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    entry = Path("src/sports_analytics/ui/streamlit_entry.py").resolve()
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(entry),
        "--server.address=127.0.0.1",
        f"--server.port={port}",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
        "--",
        "--config",
        str(config),
    ]
    process = subprocess.Popen(
        command,
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        shell=False,
    )
    deadline = time.monotonic() + 20.0
    healthy = False
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout is not None else ""
                raise AssertionError(f"Streamlit exited before health check: {output[-2000:]}")
            try:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
                connection.request("GET", "/_stcore/health")
                response = connection.getresponse()
                body = response.read()
                connection.close()
                if response.status == 200 and body.strip() == b"ok":
                    healthy = True
                    break
            except OSError:
                time.sleep(0.1)
        assert healthy, "package-native Streamlit health endpoint did not become ready"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
    assert process.poll() is not None
