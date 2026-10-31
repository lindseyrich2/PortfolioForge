"""Placeholder test confirming the package is importable."""

import portfolioforge


def test_package_has_version() -> None:
    assert isinstance(portfolioforge.__version__, str)
    assert portfolioforge.__version__
