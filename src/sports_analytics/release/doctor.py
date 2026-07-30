"""Side-effect-free local v1 release-readiness inspection."""

from __future__ import annotations

import math
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from sports_analytics import __version__
from sports_analytics.core.exceptions import SportsAnalyticsError
from sports_analytics.core.paths import RuntimePaths
from sports_analytics.core.runtime import validate_configuration
from sports_analytics.data.codec import format_utc_timestamp
from sports_analytics.data.database import connect_database, verify_sqlite_file
from sports_analytics.data.migrations import get_migration_status
from sports_analytics.governance.contracts import ModelLifecycleStatus, ModelRole
from sports_analytics.governance.repository import ModelGovernanceRepository
from sports_analytics.jobs.service import WorkerService
from sports_analytics.ui.product_catalogue import (
    discover_product_read_models,
    load_product_read_model,
)

SUPPORTED_CURRENT_PRICE_PATH: Final[str] = "strict-offline-operator-input"
PLACEMENT_MODE: Final[str] = "manual-only"
_PERSISTENT_ROLES: Final[tuple[str, ...]] = (
    "raw_directory",
    "snapshots_directory",
    "features_directory",
    "models_directory",
    "exports_directory",
)


def inspect_release_readiness(
    *,
    config_path: Path | str | None = None,
    env_file: Path | str | None = None,
    base_directory: Path | str | None = None,
) -> dict[str, Any]:
    """Return a deterministic read-only readiness report.

    This function does not bootstrap the runtime: it never creates a path,
    opens SQLite writable, configures logging, or seeds a random generator.
    """
    checks: dict[str, Any] = {}
    warnings: list[str] = []
    blockers: list[str] = []
    result: dict[str, Any] = {
        "application_version": __version__,
        "blockers": blockers,
        "bookmaker_network_required_for_v1": False,
        "checks": checks,
        "manual_placement_only": True,
        "overall_state": "invalid",
        "warnings": warnings,
    }
    python_version = tuple(int(item) for item in sys.version_info[:3])
    python_is_supported = _is_supported_python(python_version)
    checks["python"] = {
        "required": ">=3.12",
        "state": "pass" if python_is_supported else "fail",
        "version": ".".join(str(item) for item in python_version),
    }
    if not python_is_supported:
        blockers.append("Python 3.12 or later is required for the local v1 release.")
    checks["package_version"] = {"state": "pass", "value": __version__}
    try:
        settings, paths = validate_configuration(
            config_path=config_path,
            env_file=env_file,
            base_directory=base_directory,
        )
    except SportsAnalyticsError as exc:
        checks["configuration"] = {"detail": _safe_detail(exc), "state": "fail"}
        blockers.append("The complete configuration is invalid.")
        return _finish(result, "invalid")

    checks["configuration"] = {"state": "pass"}
    path_issues = inspect_path_safety(paths)
    checks["configured_path_safety"] = {
        "issues": path_issues,
        "state": "pass" if not path_issues else "fail",
    }
    if path_issues:
        blockers.append("Configured persistent paths do not satisfy the local v1 safety boundary.")

    directory_paths = {
        "storage_root": paths.storage_root,
        "sqlite_parent": paths.sqlite_path.parent,
        **{role.removesuffix("_directory"): getattr(paths, role) for role in _PERSISTENT_ROLES},
        "logs": paths.logs_directory,
    }
    directory_states = {role: path.is_dir() for role, path in directory_paths.items()}
    missing_roles = sorted(role for role, exists in directory_states.items() if not exists)
    checks["runtime_directories"] = {
        "directories": directory_states,
        "missing_roles": missing_roles,
        "state": "pass" if all(directory_states.values()) else "missing",
    }
    if missing_roles:
        warnings.append(
            "Required runtime directories are missing: " + ", ".join(missing_roles) + "."
        )

    checks["bookmakers"] = {
        "enabled": settings.bookmakers.enabled,
        "state": "enabled" if settings.bookmakers.enabled else "disabled",
    }
    if settings.bookmakers.enabled:
        warnings.append(
            "Bookmaker acquisition is explicitly enabled, but it is not required by local v1."
        )
    checks["supported_offline_current_price_path"] = {
        "path": SUPPORTED_CURRENT_PRICE_PATH,
        "state": "available",
    }
    checks["backup_destination_safety"] = {"state": "not-requested"}

    if not paths.sqlite_path.is_file():
        checks["sqlite"] = {"exists": False, "state": "not-initialized"}
        checks["migration"] = {"state": "not-initialized"}
        checks["queue"] = {"state": "unavailable"}
        checks["model_registry"] = {"state": "unavailable"}
        checks["active_champions"] = {"competitions": [], "state": "optional-data-absent"}
        warnings.append("The operational SQLite database has not been initialized.")
        _inspect_catalogue(paths, checks=checks, warnings=warnings)
        return _finish(result, "invalid" if blockers else "not-initialized")

    try:
        verify_sqlite_file(paths.sqlite_path, quick=False)
        checks["sqlite"] = {"exists": True, "integrity": "ok", "state": "pass"}
        migration = get_migration_status(paths.sqlite_path)
        checks["migration"] = {
            "applied_count": len(migration.applied),
            "current_version": migration.current_version,
            "latest_version": migration.latest_version,
            "pending_count": len(migration.pending),
            "state": "pass" if migration.is_up_to_date else "fail",
        }
        if not migration.is_up_to_date:
            blockers.append("The SQLite migration state does not match the packaged v1 schema.")
    except SportsAnalyticsError as exc:
        checks["sqlite"] = {"detail": _safe_detail(exc), "exists": True, "state": "fail"}
        checks["migration"] = {"state": "fail"}
        blockers.append("The operational SQLite database is corrupt or incompatible.")
        _inspect_catalogue(paths, checks=checks, warnings=warnings)
        return _finish(result, "invalid")

    try:
        queue = WorkerService(paths.sqlite_path, settings.worker).get_queue_status(
            observed_at=datetime.now(tz=UTC)
        )
        checks["queue"] = {
            "active_worker_count": queue.active_worker_count,
            "available_pending_count": queue.available_pending_count,
            "cancelled_count": queue.cancelled_count,
            "delayed_pending_count": queue.delayed_pending_count,
            "expired_running_lease_count": queue.expired_running_lease_count,
            "failed_count": queue.failed_count,
            "observed_at": format_utc_timestamp(queue.observed_at),
            "pending_count": queue.pending_count,
            "running_count": queue.running_count,
            "stale_worker_count": queue.stale_worker_count,
            "state": "pass",
            "succeeded_count": queue.succeeded_count,
        }
    except SportsAnalyticsError as exc:
        checks["queue"] = {"detail": _safe_detail(exc), "state": "fail"}
        blockers.append("The durable queue cannot be inspected through its database contract.")

    try:
        with connect_database(paths.sqlite_path, read_only=True) as connection:
            models = ModelGovernanceRepository(connection).list_models()
        champions = []
        for model in models:
            if (
                model.role is ModelRole.CHAMPION
                and model.lifecycle_status is ModelLifecycleStatus.PROMOTED
            ):
                provenance = model.provenance
                competition = (
                    provenance.get("competition_id") if isinstance(provenance, dict) else None
                )
                champions.append(
                    {
                        "competition_id": competition,
                        "market_key": model.market_key,
                        "model_artifact_id": model.model_artifact_id,
                    }
                )
        champions.sort(
            key=lambda item: (
                str(item["competition_id"]),
                str(item["market_key"]),
                str(item["model_artifact_id"]),
            )
        )
        checks["model_registry"] = {
            "model_count": len(models),
            "state": "pass",
        }
        checks["active_champions"] = {
            "competitions": champions,
            "state": "available" if champions else "optional-data-absent",
        }
        if not champions:
            warnings.append("No active champion is registered; this is an optional-data hold.")
    except SportsAnalyticsError as exc:
        checks["model_registry"] = {"detail": _safe_detail(exc), "state": "fail"}
        checks["active_champions"] = {"competitions": [], "state": "unavailable"}
        blockers.append("The model registry cannot be inspected.")

    _inspect_catalogue(paths, checks=checks, warnings=warnings)
    if blockers:
        return _finish(result, "invalid")
    if warnings:
        return _finish(result, "degraded")
    return _finish(result, "ready")


def inspect_path_safety(paths: RuntimePaths) -> list[str]:
    """Return deterministic path-safety findings without mutating the filesystem."""
    issues: list[str] = []
    root = _absolute_without_following(paths.storage_root)
    if root == Path(root.anchor):
        issues.append("storage root must not be a filesystem root")
    candidates = {
        "storage_root": root,
        "sqlite": _absolute_without_following(paths.sqlite_path),
        "raw": _absolute_without_following(paths.raw_directory),
        "snapshots": _absolute_without_following(paths.snapshots_directory),
        "features": _absolute_without_following(paths.features_directory),
        "models": _absolute_without_following(paths.models_directory),
        "exports": _absolute_without_following(paths.exports_directory),
        "logs": _absolute_without_following(paths.logs_directory),
    }
    for role, path in candidates.items():
        if _has_symlink_component(path):
            issues.append(f"configured path contains a symlink component: {role}")

    resolved = [path.resolve() for path in candidates.values()]
    if len(set(resolved)) != len(resolved):
        issues.append("configured runtime paths must be distinct")
    persistent_directories = [
        candidates[role].resolve()
        for role in ("raw", "snapshots", "features", "models", "exports", "logs")
    ]
    for index, candidate in enumerate(persistent_directories):
        for other_index, other in enumerate(persistent_directories):
            if index != other_index and _relative_to(candidate, other):
                issues.append("configured persistent directories must not be nested")
    resolved_root = root.resolve()
    for role, path in candidates.items():
        try:
            path.resolve().relative_to(resolved_root)
        except ValueError:
            issues.append(f"configured path is outside storage root: {role}")
    return sorted(set(issues))


def _absolute_without_following(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _has_symlink_component(path: Path) -> bool:
    current = _absolute_without_following(path)
    while True:
        if (current.exists() or current.is_symlink()) and current.is_symlink():
            return True
        if current.parent == current:
            return False
        current = current.parent


def _finish(result: dict[str, Any], overall_state: str) -> dict[str, Any]:
    result["overall_state"] = overall_state
    _assert_json_safe(result)
    return result


def _assert_json_safe(value: object) -> None:
    """Reject unsupported values instead of silently coercing doctor output."""
    if value is None or type(value) in {str, int, bool}:
        return
    if type(value) is float:
        if math.isfinite(value):
            return
        raise TypeError("doctor report cannot contain a non-finite float")
    if isinstance(value, list):
        for item in value:
            _assert_json_safe(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("doctor report JSON object keys must be strings")
            _assert_json_safe(item)
        return
    raise TypeError(f"doctor report contains unsupported JSON value: {type(value).__name__}")


def _is_supported_python(version: tuple[int, ...]) -> bool:
    return version >= (3, 12)


def _relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _inspect_catalogue(
    paths: RuntimePaths,
    *,
    checks: dict[str, Any],
    warnings: list[str],
) -> None:
    entries = discover_product_read_models(paths.exports_directory)
    valid = tuple(item for item in entries if item.is_valid)
    checks["export_catalogue"] = {
        "invalid_product_count": len(entries) - len(valid),
        "product_count": len(valid),
        "state": "pass" if paths.exports_directory.is_dir() else "optional-data-absent",
    }
    if not valid:
        checks["latest_product_state"] = {
            "current_quote_availability": "absent",
            "economic_state": "not-evaluated",
            "state": "optional-data-absent",
        }
        warnings.append("No verified persisted product read model is available.")
        return
    latest = valid[-1]
    try:
        artifact = load_product_read_model(root=paths.exports_directory, entry=latest)
        payload = artifact.payload
        assert isinstance(payload, dict)
        product = payload["product_state"]
        model = payload["model_status"]
        assert isinstance(product, dict)
        assert isinstance(model, dict)
        operational_state = str(product.get("operational_state", "unknown"))
        checks["latest_product_state"] = {
            "active_model_state": model,
            "analytical_candidate_count": _count(product, "analytical_candidate_count"),
            "current_quote_availability": (
                "absent" if operational_state == "fair-odds-only" else "persisted-state"
            ),
            "economic_state": operational_state,
            "held_candidate_count": _count(product, "held_candidate_count"),
            "placeable_manual_proposal_count": _count(product, "placeable_manual_proposal_count"),
            "product_relative_directory": latest.relative_directory,
            "rejected_candidate_count": _count(product, "rejected_candidate_count"),
            "research_only_proposal_count": _count(product, "research_only_proposal_count"),
            "state": "pass",
        }
        if operational_state == "economic-evidence-hold":
            warnings.append(
                "The latest product is under an expected economic-evidence hold; "
                "the software installation remains valid."
            )
    except (SportsAnalyticsError, AssertionError, KeyError, TypeError) as exc:
        checks["latest_product_state"] = {"detail": _safe_detail(exc), "state": "fail"}
        warnings.append("The latest product read model could not be summarized.")


def _count(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key, 0)
    return value if type(value) is int and value >= 0 else 0


def _safe_detail(exc: BaseException) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return text[:500] or type(exc).__name__
