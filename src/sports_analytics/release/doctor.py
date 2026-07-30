"""Side-effect-free local v1 release-readiness inspection."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from sports_analytics import __version__
from sports_analytics.core.exceptions import SportsAnalyticsError
from sports_analytics.core.paths import RuntimePaths
from sports_analytics.core.runtime import validate_configuration
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
    checks["python"] = {
        "required": ">=3.12",
        "state": "pass" if sys.version_info >= (3, 12) else "fail",
        "version": ".".join(str(item) for item in sys.version_info[:3]),
    }
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
        return result

    checks["configuration"] = {"state": "pass"}
    path_issues = inspect_path_safety(paths)
    checks["configured_path_safety"] = {
        "issues": path_issues,
        "state": "pass" if not path_issues else "fail",
    }
    if path_issues:
        blockers.append("Configured persistent paths do not satisfy the local v1 safety boundary.")

    directory_states = {
        role.removesuffix("_directory"): getattr(paths, role).is_dir() for role in _PERSISTENT_ROLES
    }
    directory_states["storage_root"] = paths.storage_root.is_dir()
    directory_states["logs"] = paths.logs_directory.is_dir()
    checks["runtime_directories"] = {
        "directories": directory_states,
        "state": "pass" if all(directory_states.values()) else "missing",
    }

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
        result["overall_state"] = "invalid" if blockers else "not-initialized"
        return result

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
        result["overall_state"] = "invalid"
        return result

    try:
        queue = WorkerService(paths.sqlite_path, settings.worker).get_queue_status(
            observed_at=datetime.now(tz=UTC)
        )
        checks["queue"] = {"state": "pass", **asdict(queue)}
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
        result["overall_state"] = "invalid"
    elif warnings:
        result["overall_state"] = "degraded"
    else:
        result["overall_state"] = "ready"
    return result


def inspect_path_safety(paths: RuntimePaths) -> list[str]:
    """Return deterministic path-safety findings without mutating the filesystem."""
    issues: list[str] = []
    root = paths.storage_root.resolve()
    if root == Path(root.anchor):
        issues.append("storage root must not be a filesystem root")
    candidates = [
        paths.sqlite_path,
        paths.raw_directory,
        paths.snapshots_directory,
        paths.features_directory,
        paths.models_directory,
        paths.exports_directory,
        paths.logs_directory,
    ]
    resolved = [item.resolve() for item in candidates]
    if len(set(resolved)) != len(resolved):
        issues.append("configured runtime paths must be distinct")
    persistent_directories = [
        paths.raw_directory.resolve(),
        paths.snapshots_directory.resolve(),
        paths.features_directory.resolve(),
        paths.models_directory.resolve(),
        paths.exports_directory.resolve(),
        paths.logs_directory.resolve(),
    ]
    for index, candidate in enumerate(persistent_directories):
        for other_index, other in enumerate(persistent_directories):
            if index != other_index and _relative_to(candidate, other):
                issues.append("configured persistent directories must not be nested")
    for path in candidates:
        if path.is_symlink():
            issues.append(f"configured path is a symlink: {path.name}")
        try:
            path.resolve().relative_to(root)
        except ValueError:
            issues.append(f"configured path is outside storage root: {path.name}")
    return sorted(set(issues))


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
