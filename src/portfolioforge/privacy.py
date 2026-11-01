"""Private-folder settings shared by every PortfolioForge stage.

Private folders are skipped entirely: they are never listed, nothing inside
them is recorded, and their names are replaced by ``[private]`` in results.

A set of common sensitive folder names is private by default. Each user can
add their own folders in a ``privacy.toml`` file in their settings folder
(see :func:`portfolioforge.paths.config_dir`)::

    [private_folders]
    # Absolute folders to skip entirely, with everything inside them.
    paths = ["D:/Scans/Personal"]
    # Folder names to skip wherever they appear (case-insensitive).
    names = ["Job Applications"]
    # Default names that should NOT be treated as private for you.
    allow_default_names = ["insurance"]
    # Set to false to drop the default names altogether.
    use_default_names = true

The settings file is personal; it is never stored in the repository.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import config_dir

PRIVACY_FILE_NAME = "privacy.toml"
PRIVATE_PLACEHOLDER = "[private]"

#: Folder names treated as private by default (exact, case-insensitive matches).
DEFAULT_PRIVATE_FOLDER_NAMES: frozenset[str] = frozenset(
    {
        "taxes",
        "tax returns",
        "tax documents",
        "medical",
        "medical records",
        "health records",
        "bank statements",
        "banking",
        "insurance",
        "passwords",
        "legal documents",
        "immigration",
        "pay stubs",
        "payslips",
        "personal documents",
        "private",
    }
)


class PrivacyConfigError(ValueError):
    """Raised for an invalid privacy settings file."""


@dataclass(frozen=True, slots=True)
class PrivacySettings:
    private_paths: tuple[str, ...] = ()
    """Absolute folders skipped entirely, including everything inside them."""
    private_folder_names: frozenset[str] = DEFAULT_PRIVATE_FOLDER_NAMES
    """Casefolded folder names skipped wherever they appear."""

    def is_private_name(self, name: str) -> bool:
        return name.casefold() in self.private_folder_names


def default_privacy_file() -> Path:
    return config_dir() / PRIVACY_FILE_NAME


def load_privacy_settings(path: str | os.PathLike[str] | None = None) -> PrivacySettings:
    """Load privacy settings; a missing default file means the defaults apply.

    An explicitly given ``path`` must exist.
    """
    explicit = path is not None
    file = Path(path) if explicit else default_privacy_file()
    if not file.is_file():
        if explicit:
            raise PrivacyConfigError(f"Privacy settings file not found: {file}")
        return PrivacySettings()
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PrivacyConfigError(f"Could not read privacy settings ({type(exc).__name__}).") from exc
    return settings_from_dict(data)


def settings_from_dict(data: dict[str, Any]) -> PrivacySettings:
    section = data.get("private_folders", {})
    if not isinstance(section, dict):
        raise PrivacyConfigError("[private_folders] must be a table.")
    unknown = set(section) - {"paths", "names", "allow_default_names", "use_default_names"}
    if unknown:
        raise PrivacyConfigError(f"Unknown privacy settings: {', '.join(sorted(unknown))}")

    use_defaults = section.get("use_default_names", True)
    if not isinstance(use_defaults, bool):
        raise PrivacyConfigError("use_default_names must be true or false.")
    paths = _string_list(section, "paths")
    names = {n.casefold() for n in _string_list(section, "names")}
    allowed = {n.casefold() for n in _string_list(section, "allow_default_names")}

    for raw in paths:
        if not os.path.isabs(os.path.expanduser(raw)):
            raise PrivacyConfigError("Private folder paths must be absolute.")
    base = DEFAULT_PRIVATE_FOLDER_NAMES - allowed if use_defaults else frozenset()
    return PrivacySettings(
        private_paths=tuple(os.path.abspath(os.path.expanduser(p)) for p in paths),
        private_folder_names=frozenset(base | names),
    )


def _string_list(section: dict[str, Any], key: str) -> list[str]:
    value = section.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise PrivacyConfigError(f"{key} must be a list of non-empty strings.")
    return [v.strip() for v in value]
