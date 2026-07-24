"""Analytics engine entry point (placeholder)."""

from collections.abc import Sequence

from sports_analytics.core.cli import run_component


def main(argv: Sequence[str] | None = None) -> int:
    """Bootstrap the engine component and report that analytics is not implemented."""
    return run_component(
        "engine",
        "Analytics engine entry point (placeholder).",
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())
