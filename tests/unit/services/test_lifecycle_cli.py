from __future__ import annotations

import json

from sports_analytics.players.evidence import PLAYER_IMPORT_SCHEMA
from sports_analytics.services.lifecycle_cli import main


def test_lifecycle_cli_exports_player_and_policy_templates(capsys) -> None:
    assert main(["--export-player-json-template"]) == 0
    player = json.loads(capsys.readouterr().out)
    assert player["schema_version"] == PLAYER_IMPORT_SCHEMA
    assert main(["--export-proposal-policy-template"]) == 0
    policy = json.loads(capsys.readouterr().out)
    assert policy["allowed_sports"] == ["football"]
    assert policy["combination_mode"] == "combine-selected-sports"


def test_lifecycle_cli_inspects_player_capabilities_without_runtime(capsys) -> None:
    assert main(["--inspect-player-capability"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert ["team-level-player-features", "player-context-not-trainable"] in payload["capabilities"]
    assert ["anytime-scorer", "player-data-required"] in payload["capabilities"]
