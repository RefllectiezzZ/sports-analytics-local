"""Repository compatibility entry point for the package-native local v1 CLI."""

from collections.abc import Sequence

from sports_analytics.release.cli import main as release_main


def main(argv: Sequence[str] | None = None) -> int:
    """Run the supported local v1 operator command."""
    return release_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
