"""Bounded operator flow plus localhost Streamlit lifecycle."""

from __future__ import annotations

import http.client
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from sports_analytics.mvp.operator_inputs import (
    build_match_options,
    publish_human_matches,
    validate_human_matches,
    validate_human_odds,
)
from sports_analytics.policies.proposal import (
    PublishedProposalPolicy,
    publish_proposal_policy,
)
from sports_analytics.release.cli import initialize_v1
from sports_analytics.services.production_football_product import (
    ProductionFootballProductRequest,
    run_and_publish_production_football_product,
)
from tests.helpers_snapshots import build_verified_participant_registry
from tests.unit.services.test_production_product_boundary import (
    MARKET_KEY,
    _connection,
    _register_champion,
)

NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


def test_mvp_human_operator_flow_and_streamlit_health(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    exports = runtime / "exports"
    models = runtime / "models"
    config = _write_config(tmp_path)
    initialize_v1(config_path=config, base_directory=tmp_path)
    registry_artifact, registry, _reference = build_verified_participant_registry(
        tmp_path,
        root=exports,
        canonical_participant_ids=(
            "11111111-1111-5111-8111-111111111111",
            "22222222-2222-5222-8222-222222222222",
        ),
        relative_directory="mvp/participants",
        evaluated_at_utc=NOW,
    )
    teams = registry.participants_for_competition("prt-primeira-liga")
    match_validation = validate_human_matches(
        (
            {
                "competition": "prt-primeira-liga",
                "home_team": teams[0].canonical_display_name,
                "away_team": teams[1].canonical_display_name,
                "scheduled_time": "2026-08-15T19:00:00Z",
                "external_source_label": "integration fixture",
            },
        ),
        registry=registry,
        evaluated_at_utc=NOW,
    )
    assert match_validation.is_valid
    event_artifact = publish_human_matches(
        match_validation,
        root=exports,
        registry=registry,
        evaluated_at_utc=NOW,
    )[0]
    options = build_match_options(match_validation.events, registry=registry)
    odds = validate_human_odds(
        tuple(
            {
                "provider": "betano-pt",
                "match": options[0].label,
                "market": "match-result",
                "outcome": outcome,
                "line": "",
                "decimal_odds": odd,
                "observed_timestamp": "2026-08-01T12:00:00Z",
            }
            for outcome, odd in (
                ("home", "2.20"),
                ("draw", "3.50"),
                ("away", "3.60"),
            )
        ),
        match_options=options,
        registered_provider_ids=frozenset({"betano-pt"}),
        evaluated_at_utc=NOW,
    )
    assert odds.is_valid
    policy = publish_proposal_policy(
        root=exports,
        relative_directory="mvp/policy",
        policy=PublishedProposalPolicy(),
    )
    connection = _connection()
    _register_champion(connection, models, match_validation.events)
    product = run_and_publish_production_football_product(
        connection=connection,
        exports_root=exports,
        model_root=models,
        request=ProductionFootballProductRequest(
            upcoming_event_relative_directory=event_artifact.relative_directory,
            upcoming_event_artifact_id=event_artifact.artifact_id,
            upcoming_event_checksum_sha256=event_artifact.checksum_sha256,
            participant_registry_relative_directory=registry_artifact.relative_directory,
            participant_registry_artifact_id=registry_artifact.artifact_id,
            participant_registry_checksum_sha256=registry_artifact.checksum_sha256,
            competition_id="prt-primeira-liga",
            market_key=MARKET_KEY,
            evaluated_at_utc=NOW,
            relative_root="mvp/integration-product",
            proposal_policy_relative_directory=policy.relative_directory,
            proposal_policy_checksum_sha256=policy.checksum_sha256,
            operator_quotes=odds.inputs,
            registered_provider_ids=frozenset({"betano-pt"}),
        ),
    )
    assert product.proposals is not None
    assert product.proposals.decisions
    assert any(
        item.offered_decimal_odds is not None or item.reason_codes
        for item in product.proposals.decisions
    )
    connection.close()

    port = _free_port()
    entry = Path("src/sports_analytics/ui/streamlit_entry.py").resolve()
    process = subprocess.Popen(
        [
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
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        shell=False,
    )
    try:
        assert _wait_for_health(process, port)
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


def _write_config(base: Path) -> Path:
    config = base / "settings.toml"
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
    return config


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_health(process: subprocess.Popen[str], port: int) -> bool:
    deadline = time.monotonic() + 20.0
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
                return True
        except OSError:
            time.sleep(0.1)
    return False
