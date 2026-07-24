"""Background worker entry point (placeholder)."""

from collections.abc import Sequence

from sports_analytics.core.cli import run_component


def main(argv: Sequence[str] | None = None) -> int:
    """Bootstrap the worker component and report that the worker is not implemented."""
    return run_component(
        "worker",
        "Background worker entry point (placeholder).",
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())
