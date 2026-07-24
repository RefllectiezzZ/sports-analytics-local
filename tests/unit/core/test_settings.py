"""Unit tests for typed configuration loading and precedence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sports_analytics.core.exceptions import ConfigurationError
from sports_analytics.core.settings import (
    Settings,
    deep_merge,
    environ_to_nested_mapping,
    load_settings,
)


def test_builtin_defaults_load_successfully(isolated_base: Path) -> None:
    settings = load_settings(environ={}, base_directory=isolated_base)
    assert settings.application.name == "sports-analytics-local"
    assert settings.application.environment == "development"
    assert settings.storage.root_directory == Path("storage")
    assert settings.logging.file_name == "sports-analytics.log"
    assert settings.worker.poll_interval_seconds == 30
    assert settings.worker.retry_backoff_base_seconds == 5
    assert settings.worker.retry_backoff_max_seconds == 300
    assert settings.worker.shutdown_grace_seconds == 30
    assert settings.worker.recovery_batch_size == 100


def test_missing_default_settings_toml_does_not_fail(tmp_path: Path) -> None:
    settings = load_settings(environ={}, base_directory=tmp_path)
    assert isinstance(settings, Settings)


def test_explicit_missing_toml_fails(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"
    with pytest.raises(ConfigurationError, match="configuration file not found"):
        load_settings(config_path=missing, environ={}, base_directory=tmp_path)


def test_environment_selected_missing_toml_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="configuration file not found"):
        load_settings(
            environ={"SPORTS_ANALYTICS_CONFIG_PATH": "does-not-exist.toml"},
            base_directory=tmp_path,
        )


def test_explicit_missing_env_file_fails(tmp_path: Path) -> None:
    missing = tmp_path / "missing.env"
    with pytest.raises(ConfigurationError, match="environment file not found"):
        load_settings(env_file=missing, environ={}, base_directory=tmp_path)


def test_absent_default_env_does_not_fail(tmp_path: Path) -> None:
    settings = load_settings(environ={}, base_directory=tmp_path)
    assert settings.application.environment == "development"


def test_valid_toml_values_load(tmp_path: Path) -> None:
    config = tmp_path / "settings.toml"
    config.write_text(
        '[application]\nenvironment = "production"\ntimezone = "Europe/Lisbon"\n',
        encoding="utf-8",
    )
    settings = load_settings(config_path=config, environ={}, base_directory=tmp_path)
    assert settings.application.environment == "production"
    assert settings.application.timezone == "Europe/Lisbon"


def test_invalid_toml_raises_configuration_error(tmp_path: Path) -> None:
    config = tmp_path / "broken.toml"
    config.write_text("this is = not [ valid", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="invalid TOML"):
        load_settings(config_path=config, environ={}, base_directory=tmp_path)


def test_empty_toml_is_accepted(tmp_path: Path) -> None:
    config = tmp_path / "empty.toml"
    config.write_text("", encoding="utf-8")
    settings = load_settings(config_path=config, environ={}, base_directory=tmp_path)
    assert settings.application.deterministic_seed == 42


def test_unknown_toml_field_rejected(tmp_path: Path) -> None:
    config = tmp_path / "settings.toml"
    config.write_text('[application]\nunknown_field = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="validation failed"):
        load_settings(config_path=config, environ={}, base_directory=tmp_path)


def test_unknown_toml_section_rejected(tmp_path: Path) -> None:
    config = tmp_path / "settings.toml"
    config.write_text("[mystery]\nenabled = true\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="validation failed"):
        load_settings(config_path=config, environ={}, base_directory=tmp_path)


def test_valid_env_values_load(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SPORTS_ANALYTICS_APPLICATION__ENVIRONMENT=test\nSPORTS_ANALYTICS_LOGGING__LEVEL=DEBUG\n",
        encoding="utf-8",
    )
    settings = load_settings(env_file=env_file, environ={}, base_directory=tmp_path)
    assert settings.application.environment == "test"
    assert settings.logging.level == "DEBUG"


def test_os_environment_overrides_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SPORTS_ANALYTICS_APPLICATION__ENVIRONMENT=development\n",
        encoding="utf-8",
    )
    settings = load_settings(
        env_file=env_file,
        environ={"SPORTS_ANALYTICS_APPLICATION__ENVIRONMENT": "production"},
        base_directory=tmp_path,
    )
    assert settings.application.environment == "production"


def test_env_file_overrides_toml(tmp_path: Path) -> None:
    config = tmp_path / "settings.toml"
    config.write_text('[application]\nenvironment = "development"\n', encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SPORTS_ANALYTICS_APPLICATION__ENVIRONMENT=test\n",
        encoding="utf-8",
    )
    settings = load_settings(
        config_path=config,
        env_file=env_file,
        environ={},
        base_directory=tmp_path,
    )
    assert settings.application.environment == "test"


def test_toml_overrides_builtin_defaults(tmp_path: Path) -> None:
    config = tmp_path / "settings.toml"
    config.write_text(
        "[worker]\npoll_interval_seconds = 11\n"
        "heartbeat_interval_seconds = 5\nstale_job_timeout_seconds = 60\n",
        encoding="utf-8",
    )
    settings = load_settings(config_path=config, environ={}, base_directory=tmp_path)
    assert settings.worker.poll_interval_seconds == 11
    assert settings.worker.heartbeat_interval_seconds == 5
    assert settings.worker.stale_job_timeout_seconds == 60


def test_new_worker_settings_load_from_toml_and_env(tmp_path: Path) -> None:
    config = tmp_path / "settings.toml"
    config.write_text(
        "[worker]\n"
        "retry_backoff_base_seconds = 2\n"
        "retry_backoff_max_seconds = 20\n"
        "shutdown_grace_seconds = 4\n"
        "recovery_batch_size = 7\n",
        encoding="utf-8",
    )
    settings = load_settings(
        config_path=config,
        environ={
            "SPORTS_ANALYTICS_WORKER__RETRY_BACKOFF_MAX_SECONDS": "30",
            "SPORTS_ANALYTICS_WORKER__RECOVERY_BATCH_SIZE": "9",
        },
        base_directory=tmp_path,
    )
    assert settings.worker.retry_backoff_base_seconds == 2
    assert settings.worker.retry_backoff_max_seconds == 30
    assert settings.worker.shutdown_grace_seconds == 4
    assert settings.worker.recovery_batch_size == 9


def test_explicit_overrides_beat_all_sources(tmp_path: Path) -> None:
    config = tmp_path / "settings.toml"
    config.write_text('[application]\nenvironment = "development"\n', encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SPORTS_ANALYTICS_APPLICATION__ENVIRONMENT=test\n",
        encoding="utf-8",
    )
    settings = load_settings(
        config_path=config,
        env_file=env_file,
        environ={"SPORTS_ANALYTICS_APPLICATION__ENVIRONMENT": "production"},
        overrides={"application": {"environment": "test"}},
        base_directory=tmp_path,
    )
    assert settings.application.environment == "test"


def test_complete_precedence_chain(tmp_path: Path) -> None:
    config = tmp_path / "settings.toml"
    config.write_text(
        '[application]\nname = "from-toml"\nenvironment = "development"\n'
        'timezone = "UTC"\ndeterministic_seed = 1\n',
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SPORTS_ANALYTICS_APPLICATION__NAME=from-dotenv\n"
        "SPORTS_ANALYTICS_APPLICATION__ENVIRONMENT=test\n"
        "SPORTS_ANALYTICS_APPLICATION__DETERMINISTIC_SEED=2\n",
        encoding="utf-8",
    )
    settings = load_settings(
        config_path=config,
        env_file=env_file,
        environ={
            "SPORTS_ANALYTICS_APPLICATION__ENVIRONMENT": "production",
            "SPORTS_ANALYTICS_APPLICATION__DETERMINISTIC_SEED": "3",
            "UNRELATED_VALUE": "ignored",
        },
        overrides={"application": {"deterministic_seed": 4}},
        base_directory=tmp_path,
    )
    assert settings.application.name == "from-dotenv"
    assert settings.application.environment == "production"
    assert settings.application.timezone == "UTC"
    assert settings.application.deterministic_seed == 4


def test_unrelated_environment_variables_ignored(tmp_path: Path) -> None:
    settings = load_settings(
        environ={"PATH": "/usr/bin", "HOME": "/tmp", "FOO": "bar"},
        base_directory=tmp_path,
    )
    assert settings.application.environment == "development"


def test_unknown_sports_analytics_variables_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        load_settings(
            environ={"SPORTS_ANALYTICS_NOT_A_REAL_SECTION__VALUE": "1"},
            base_directory=tmp_path,
        )


def test_nested_environment_names_interpreted(tmp_path: Path) -> None:
    settings = load_settings(
        environ={
            "SPORTS_ANALYTICS_APPLICATION__TIMEZONE": "Europe/Lisbon",
            "SPORTS_ANALYTICS_LOGGING__LEVEL": "WARNING",
            "SPORTS_ANALYTICS_WORKER__POLL_INTERVAL_SECONDS": "10",
        },
        base_directory=tmp_path,
    )
    assert settings.application.timezone == "Europe/Lisbon"
    assert settings.logging.level == "WARNING"
    assert settings.worker.poll_interval_seconds == 10


def test_booleans_and_integers_parsed(tmp_path: Path) -> None:
    settings = load_settings(
        environ={
            "SPORTS_ANALYTICS_SCRAPING__ENABLED": "true",
            "SPORTS_ANALYTICS_SCRAPING__MAXIMUM_RETRIES": "7",
            "SPORTS_ANALYTICS_LOGGING__FILE_ENABLED": "false",
        },
        base_directory=tmp_path,
    )
    assert settings.scraping.enabled is True
    assert settings.scraping.maximum_retries == 7
    assert settings.logging.file_enabled is False


def test_invalid_boolean_input_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        load_settings(
            environ={"SPORTS_ANALYTICS_SCRAPING__ENABLED": "maybe"},
            base_directory=tmp_path,
        )


def test_invalid_timezone_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="timezone"):
        load_settings(
            environ={"SPORTS_ANALYTICS_APPLICATION__TIMEZONE": "Not/AZone"},
            base_directory=tmp_path,
        )


def test_invalid_log_level_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        load_settings(
            environ={"SPORTS_ANALYTICS_LOGGING__LEVEL": "VERBOSE"},
            base_directory=tmp_path,
        )


def test_invalid_log_file_name_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="file_name"):
        load_settings(
            overrides={"logging": {"file_name": "../escape.log"}},
            environ={},
            base_directory=tmp_path,
        )


def test_invalid_worker_timing_relationship_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="stale_job_timeout_seconds"):
        load_settings(
            overrides={
                "worker": {
                    "poll_interval_seconds": 30,
                    "heartbeat_interval_seconds": 60,
                    "stale_job_timeout_seconds": 30,
                }
            },
            environ={},
            base_directory=tmp_path,
        )


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), float("-inf")])
def test_worker_timing_rejects_non_finite_and_boolean_values(
    tmp_path: Path,
    value: object,
) -> None:
    with pytest.raises(ConfigurationError, match="positive finite numbers|must not be boolean"):
        load_settings(
            overrides={"worker": {"poll_interval_seconds": value}},
            environ={},
            base_directory=tmp_path,
        )


def test_invalid_worker_retry_backoff_relationship_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="retry_backoff_max_seconds"):
        load_settings(
            overrides={
                "worker": {
                    "retry_backoff_base_seconds": 60,
                    "retry_backoff_max_seconds": 30,
                }
            },
            environ={},
            base_directory=tmp_path,
        )


def test_invalid_worker_recovery_batch_size_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="recovery_batch_size"):
        load_settings(
            overrides={"worker": {"recovery_batch_size": 0}},
            environ={},
            base_directory=tmp_path,
        )
    with pytest.raises(ConfigurationError, match="recovery_batch_size"):
        load_settings(
            overrides={"worker": {"recovery_batch_size": True}},
            environ={},
            base_directory=tmp_path,
        )


def test_model_instances_are_immutable() -> None:
    from pydantic import ValidationError

    settings = Settings()
    with pytest.raises(ValidationError):
        settings.application.environment = "production"  # type: ignore[misc]


def test_settings_modules_have_no_import_side_effects(tmp_path: Path) -> None:
    # Importing settings helpers must not create storage directories.
    assert not (tmp_path / "storage").exists()
    _ = load_settings
    assert not (tmp_path / "storage").exists()


def test_input_mappings_are_not_mutated() -> None:
    base: dict[str, Any] = {"application": {"environment": "development"}}
    override: dict[str, Any] = {"application": {"environment": "test"}}
    merged = deep_merge(base, override)
    assert base["application"]["environment"] == "development"
    assert override["application"]["environment"] == "test"
    assert merged["application"]["environment"] == "test"


def test_repeated_loads_with_identical_inputs_are_equal(tmp_path: Path) -> None:
    kwargs = {
        "environ": {"SPORTS_ANALYTICS_LOGGING__LEVEL": "ERROR"},
        "base_directory": tmp_path,
    }
    first = load_settings(**kwargs)
    second = load_settings(**kwargs)
    assert first == second


def test_config_path_from_dotenv(tmp_path: Path) -> None:
    config = tmp_path / "selected.toml"
    config.write_text('[application]\nenvironment = "production"\n', encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"SPORTS_ANALYTICS_CONFIG_PATH={config.as_posix()}\n",
        encoding="utf-8",
    )
    settings = load_settings(env_file=env_file, environ={}, base_directory=tmp_path)
    assert settings.application.environment == "production"


def test_explicit_config_path_beats_env_selection(tmp_path: Path) -> None:
    via_env = tmp_path / "via-env.toml"
    via_env.write_text('[application]\nenvironment = "production"\n', encoding="utf-8")
    via_arg = tmp_path / "via-arg.toml"
    via_arg.write_text('[application]\nenvironment = "test"\n', encoding="utf-8")
    settings = load_settings(
        config_path=via_arg,
        environ={"SPORTS_ANALYTICS_CONFIG_PATH": str(via_env)},
        base_directory=tmp_path,
    )
    assert settings.application.environment == "test"


def test_environ_to_nested_mapping_ignores_unrelated() -> None:
    nested = environ_to_nested_mapping(
        {
            "PATH": "/bin",
            "SPORTS_ANALYTICS_CONFIG_PATH": "config/settings.toml",
            "SPORTS_ANALYTICS_LOGGING__LEVEL": "DEBUG",
        }
    )
    assert nested == {"logging": {"level": "DEBUG"}}


def test_deep_merge_type_conflict() -> None:
    with pytest.raises(ConfigurationError, match="type conflict"):
        deep_merge({"application": {"name": "x"}}, {"application": "bad"})


def test_example_settings_file_validates(isolated_base: Path) -> None:
    example = Path("config/settings.example.toml").resolve()
    settings = load_settings(
        config_path=example,
        environ={},
        base_directory=isolated_base,
    )
    assert settings.logging.date_format
    assert settings.logging.file_name == "sports-analytics.log"


def test_dotenv_interpolation_disabled(
    isolated_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXTERNAL_NAME", "from-process-env")
    env_file = isolated_base / ".env"
    env_file.write_text(
        "SPORTS_ANALYTICS_APPLICATION__NAME=${EXTERNAL_NAME}\n",
        encoding="utf-8",
    )
    first = load_settings(
        env_file=env_file,
        environ={},
        base_directory=isolated_base,
    )
    assert first.application.name == "${EXTERNAL_NAME}"

    monkeypatch.setenv("EXTERNAL_NAME", "changed-process-env")
    second = load_settings(
        env_file=env_file,
        environ={},
        base_directory=isolated_base,
    )
    assert second.application.name == "${EXTERNAL_NAME}"
    assert first == second


def test_hostile_repository_dotenv_ignored_with_isolated_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    (fake_repo / ".env").write_text(
        "SPORTS_ANALYTICS_APPLICATION__ENVIRONMENT=not-a-valid-environment\n",
        encoding="utf-8",
    )
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    monkeypatch.chdir(fake_repo)
    settings = load_settings(environ={}, base_directory=isolated)
    assert settings.application.environment == "development"


def test_invalid_utf8_toml_raises_configuration_error(isolated_base: Path) -> None:
    config = isolated_base / "broken.toml"
    config.write_bytes(b"\xff\xfe[application]\n")
    with pytest.raises(ConfigurationError, match="TOML configuration file") as exc_info:
        load_settings(config_path=config, environ={}, base_directory=isolated_base)
    assert isinstance(exc_info.value.__cause__, UnicodeError)


def test_invalid_utf8_dotenv_raises_configuration_error(isolated_base: Path) -> None:
    env_file = isolated_base / "broken.env"
    env_file.write_bytes(b"SPORTS_ANALYTICS_APPLICATION__NAME=\xff\xfe\n")
    with pytest.raises(ConfigurationError, match="environment file") as exc_info:
        load_settings(env_file=env_file, environ={}, base_directory=isolated_base)
    assert isinstance(exc_info.value.__cause__, UnicodeError)


def test_invalid_logging_format_fails(isolated_base: Path) -> None:
    with pytest.raises(ConfigurationError, match="logging.format"):
        load_settings(
            overrides={"logging": {"format": "%(asctime"}},
            environ={},
            base_directory=isolated_base,
        )
