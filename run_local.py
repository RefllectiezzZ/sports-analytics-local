"""Local startup coordinator entry point (placeholder)."""

from collections.abc import Sequence

from sports_analytics.core.cli import run_component


def main(argv: Sequence[str] | None = None) -> int:
    """Bootstrap run_local and report that process coordination is not implemented."""
    return run_component(
        "run_local",
        "Local startup coordinator entry point (placeholder).",
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())
