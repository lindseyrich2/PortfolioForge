"""Per-user storage locations, chosen by platform convention.

PortfolioForge data (inventories, grouping results, review decisions) contains
the names and paths of personal files, so it is kept in the user's own
application-data folder, never in a Git repository.

=========  ==============================================  ==========================================
Platform   Data                                            Settings
=========  ==============================================  ==========================================
Windows    ``%LOCALAPPDATA%\\PortfolioForge``              ``%APPDATA%\\PortfolioForge``
macOS      ``~/Library/Application Support/PortfolioForge``  same folder
Linux      ``$XDG_DATA_HOME/portfolioforge``                 ``$XDG_CONFIG_HOME/portfolioforge``
           (default ``~/.local/share/portfolioforge``)        (default ``~/.config/portfolioforge``)
=========  ==============================================  ==========================================

``PORTFOLIOFORGE_DATA_DIR`` and ``PORTFOLIOFORGE_CONFIG_DIR`` override these.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DATA_DIR_ENV = "PORTFOLIOFORGE_DATA_DIR"
CONFIG_DIR_ENV = "PORTFOLIOFORGE_CONFIG_DIR"


class UnsafeOutputLocation(ValueError):
    """Raised when PortfolioForge data would be written somewhere unsafe."""


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def data_dir() -> Path:
    if override := os.environ.get(DATA_DIR_ENV):
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) / "PortfolioForge" if base else _home() / "AppData" / "Local" / "PortfolioForge"
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support" / "PortfolioForge"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else _home() / ".local" / "share") / "portfolioforge"


def config_dir() -> Path:
    if override := os.environ.get(CONFIG_DIR_ENV):
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return Path(base) / "PortfolioForge" if base else _home() / "AppData" / "Roaming" / "PortfolioForge"
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support" / "PortfolioForge"
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else _home() / ".config") / "portfolioforge"


def inventories_dir() -> Path:
    return data_dir() / "inventories"


def find_git_work_tree(path: str | os.PathLike[str]) -> Path | None:
    """The Git working tree containing ``path`` (existing or not), if any.

    Checks only whether a ``.git`` entry exists in each ancestor folder.
    """
    current = Path(os.path.abspath(os.fspath(path)))
    for candidate in (current, *current.parents):
        if os.path.lexists(candidate / ".git"):
            return candidate
    return None


def ensure_safe_output_dir(path: str | os.PathLike[str]) -> Path:
    """Refuse to store PortfolioForge data inside a Git working tree."""
    target = Path(os.path.abspath(os.fspath(path)))
    if find_git_work_tree(target) is not None:
        raise UnsafeOutputLocation(
            "Refusing to write PortfolioForge data inside a Git repository, where it could be "
            "committed by mistake. Choose a folder outside any repository, or use the default."
        )
    return target
