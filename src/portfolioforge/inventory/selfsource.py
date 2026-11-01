"""Locate PortfolioForge's own source code so scans can leave it out.

Nothing here is tied to a particular user or path: the running checkout is
found from this module's own location, and other checkouts (forks, extra
clones) are recognized by their folder layout. Only folder metadata is used.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import AbstractSet

#: Folder layout of a PortfolioForge checkout, relative to its root.
_PACKAGE_PARTS = ("src", "portfolioforge", "inventory")


def portfolioforge_source_dir() -> str | None:
    """Root of the source checkout this copy of PortfolioForge runs from.

    Returns None for a regular (non-editable) install: that code lives in
    ``site-packages``, which scans already skip.
    """
    package_dir = Path(__file__).resolve().parent.parent  # .../portfolioforge
    src_dir = package_dir.parent
    if src_dir.name.casefold() != "src":
        return None
    checkout = src_dir.parent
    return str(checkout) if (checkout / "pyproject.toml").is_file() else None


def looks_like_portfolioforge_checkout(path: str, names: AbstractSet[str]) -> bool:
    """True if a folder with these (casefolded) entry names is a PortfolioForge checkout."""
    if "pyproject.toml" not in names or "src" not in names:
        return False
    return os.path.isdir(os.path.join(path, *_PACKAGE_PARTS))
