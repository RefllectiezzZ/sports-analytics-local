"""Smoke tests for the sports_analytics package metadata."""

import sports_analytics


def test_version_is_defined() -> None:
    """Package version must be present and equal the bootstrap release."""
    assert hasattr(sports_analytics, "__version__")
    assert isinstance(sports_analytics.__version__, str)
    assert sports_analytics.__version__
    assert sports_analytics.__version__ == "1.0.0"
