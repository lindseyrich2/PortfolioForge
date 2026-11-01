"""Scan options and resource limits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_FILES = 1_000_000
DEFAULT_MAX_DIRECTORIES = 2_000_000
DEFAULT_MAX_DEPTH = 64
DEFAULT_TIME_BUDGET_SECONDS = 30 * 60.0


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Bounds on how much work one scan may do. ``None`` disables a limit."""

    max_files: int | None = DEFAULT_MAX_FILES
    """Stop the scan once this many files have been inventoried."""
    max_directories: int | None = DEFAULT_MAX_DIRECTORIES
    """Stop the scan once this many folders have been visited."""
    max_depth: int | None = DEFAULT_MAX_DEPTH
    """Folders nested deeper than this below a root are skipped (and reported)."""
    time_budget_seconds: float | None = DEFAULT_TIME_BUDGET_SECONDS
    """Stop the scan after this much wall-clock time."""
    progress_interval_seconds: float = 1.0
    throttle_every: int = 1000
    """Pause after every this many directory entries when throttling is on."""
    throttle_seconds: float = 0.0
    """Length of each pause; 0 disables throttling."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResourceLimits:
        return cls(**data)


@dataclass(frozen=True, slots=True)
class InventoryConfig:
    """What to inventory and which default exclusions to apply.

    The protected tier (system folders, credential locations, sensitive files,
    links, cloud-only placeholders) always applies and cannot be turned off here.
    """

    include_images: bool = False
    """Inventory image files (``.png``, ``.jpg``, ...). Off by default."""
    skip_build_dirs: bool = True
    """Skip compiler/packaging output folders such as ``build`` and ``out``."""
    skip_installed_tools: bool = True
    """Skip installed tools such as ``anaconda3`` or a folder with an uninstaller."""
    skip_r_libraries: bool = True
    """Skip installed R package libraries (win-library, renv/library, packrat/lib,
    and any folder laid out as an installed R package). R scripts and R package
    source code are still inventoried."""
    skip_portfolioforge_source: bool = True
    """Skip PortfolioForge's own source code: the checkout this copy runs from,
    and any other folder laid out as a PortfolioForge checkout."""
    include_dir_names: frozenset[str] = frozenset()
    """Folder names exempt from the default-tier exclusions (case-insensitive)."""
    exclude_dir_names: frozenset[str] = frozenset()
    """Additional folder names to skip (case-insensitive)."""
    exclude_paths: tuple[str, ...] = ()
    """Absolute folder paths to skip, including everything below them."""
    extra_sensitive_patterns: tuple[str, ...] = ()
    """Additional filename glob patterns to treat as sensitive."""


@dataclass(frozen=True, slots=True)
class FullScanOptions:
    """Explicit opt-ins for a full-computer scan plan."""

    include_drives: tuple[str, ...] = ()
    """Additional drive letters to scan, e.g. ``("D",)``. None by default."""
    include_locations: tuple[str, ...] = ()
    """Optional locations (other or old profiles) or extra folders to scan."""
