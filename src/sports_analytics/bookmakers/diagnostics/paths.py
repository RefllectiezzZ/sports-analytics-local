"""Safe diagnostic directory resolution."""

from __future__ import annotations

from pathlib import Path

from sports_analytics.core.exceptions import ConfigurationError

DEFAULT_DIAGNOSTIC_DIRECTORY = Path("storage/local/bookmaker-diagnostics")


def resolve_diagnostic_directory(
    requested: str | Path | None,
    *,
    base_directory: Path | None = None,
) -> Path:
    """Resolve and validate a local-only diagnostic output directory."""
    base = (base_directory or Path.cwd()).resolve()
    target = DEFAULT_DIAGNOSTIC_DIRECTORY if requested is None else Path(requested)
    if target.is_absolute():
        resolved = target.resolve()
    else:
        resolved = (base / target).resolve()
    if not _is_within(resolved, base):
        msg = "diagnostic directory must stay within the project workspace"
        raise ConfigurationError(msg)
    if resolved.is_symlink():
        msg = "diagnostic directory must not be a symlink"
        raise ConfigurationError(msg)
    if resolved.exists():
        if resolved.is_symlink():
            msg = "diagnostic directory must not be a symlink"
            raise ConfigurationError(msg)
        for parent in resolved.parents:
            if parent.is_symlink():
                msg = "diagnostic directory path must not traverse symlinks"
                raise ConfigurationError(msg)
            if parent == base:
                break
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    parts = path.parts
    if ".." in parts:
        return False
    return True
