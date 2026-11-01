"""Structured inventory output.

All models are frozen dataclasses. ``Inventory.to_dict()`` / ``to_json()``
produce JSON-safe data and ``Inventory.from_dict()`` reloads it, so later
pipeline stages can work from a saved inventory without rescanning.

Relative paths are relative to a scan root, always use ``/`` as the separator,
and the root itself is ``""``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from enum import Enum, StrEnum
from typing import Any

from .config import ResourceLimits
from .formats import Category

SCHEMA_VERSION = 1


class ScanMode(StrEnum):
    TARGETED = "targeted"
    FULL_COMPUTER = "full_computer"


class RootOrigin(StrEnum):
    USER_SELECTED = "user_selected"
    PROFILE = "profile"
    KNOWN_FOLDER = "known_folder"
    DRIVE_TOP_LEVEL = "drive_top_level"
    ADDITIONAL_DRIVE = "additional_drive"
    OTHER_PROFILE = "other_profile"


class EntryKind(StrEnum):
    DIRECTORY = "directory"
    FILE = "file"
    LINK = "link"


class SkipReason(StrEnum):
    # protected tier
    PROTECTED_LOCATION = "protected_location"
    CREDENTIAL_LOCATION = "credential_location"
    SENSITIVE_NAME = "sensitive_name"
    LINK = "link"
    CLOUD_ONLY = "cloud_only"
    OTHER_PROFILE = "other_profile"
    OUTPUT_DIR = "output_dir"
    # default tier
    HIDDEN = "hidden"
    SYSTEM = "system"
    IGNORED_NAME = "ignored_name"
    VIRTUAL_ENV = "virtual_env"
    BUILD_DIR = "build_dir"
    INSTALLED_TOOL = "installed_tool"
    R_PACKAGE_LIBRARY = "r_package_library"
    PORTFOLIOFORGE_SOURCE = "portfolioforge_source"
    OFFICE_LOCK_FILE = "office_lock_file"
    OS_METADATA_FILE = "os_metadata_file"
    # user settings, limits and bookkeeping
    USER_EXCLUDED = "user_excluded"
    MAX_DEPTH = "max_depth"
    ALREADY_SCANNED = "already_scanned"
    OVERLAPPING_ROOT = "overlapping_root"
    # errors
    PERMISSION_ERROR = "permission_error"
    OS_ERROR = "os_error"


ERROR_REASONS = frozenset({SkipReason.PERMISSION_ERROR, SkipReason.OS_ERROR})


@dataclass(frozen=True, slots=True)
class ScanRoot:
    id: str
    """Stable identifier derived from the root's path."""
    path: str
    origin: RootOrigin
    label: str
    """Human-readable location, e.g. ``"Downloads"`` or ``"C:\\CS101"``."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScanRoot:
        return cls(data["id"], data["path"], RootOrigin(data["origin"]), data["label"])


@dataclass(frozen=True, slots=True)
class PlanExclusion:
    path: str
    reason: SkipReason


@dataclass(frozen=True, slots=True)
class OptionalLocation:
    """A location that was detected but is only scanned after explicit approval."""

    path: str
    kind: str
    """``additional_drive``, ``other_profile`` or ``old_windows_profile``."""
    description: str
    includable: bool
    include_hint: str | None
    """Command-line option that opts this location in, if it can be included."""


@dataclass(frozen=True, slots=True)
class ScanPlan:
    """Everything a scan will cover, shown to the user before it runs."""

    mode: ScanMode
    roots: tuple[ScanRoot, ...]
    excluded: tuple[PlanExclusion, ...]
    optional: tuple[OptionalLocation, ...]
    profile_dir: str | None
    users_dirs: tuple[str, ...]
    """Folders that hold user profiles; other accounts' profiles inside are skipped."""
    drive_roots: tuple[str, ...]
    """Drive roots; protected top-level folder names are matched directly below these."""
    output_dir: str | None

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class FileRecord:
    """One inventoried file, built from directory metadata only (never contents)."""

    id: str
    """Stable identifier derived from the root and relative path, not contents."""
    root_id: str
    relative_path: str
    absolute_path: str
    name: str
    stem: str
    extension: str
    """Lowercase extension including the dot: ``analysis.R`` → ``".r"``."""
    category: Category
    language: str | None
    parent_dir: str
    """Relative path of the containing folder; ``""`` for the root."""
    path_parts: tuple[str, ...]
    """Folder chain from the root to the containing folder."""
    depth: int
    """Number of folders between the root and the file."""
    size_bytes: int
    modified_at: datetime | None
    created_at: datetime | None
    """Creation time where the OS reports it (Windows, macOS); otherwise None."""
    location_hint: str
    """Label of the root the file was found under."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileRecord:
        return cls(
            **{
                **data,
                "category": Category(data["category"]),
                "path_parts": tuple(data["path_parts"]),
                "modified_at": _parse_datetime(data["modified_at"]),
                "created_at": _parse_datetime(data["created_at"]),
            }
        )


@dataclass(frozen=True, slots=True)
class DirectoryRecord:
    """One folder that was traversed. Skipped folders appear in ``skipped``."""

    root_id: str
    relative_path: str
    absolute_path: str
    name: str
    parent_dir: str | None
    """Relative path of the parent folder; None for the root."""
    depth: int
    file_count: int
    """Files inventoried directly in this folder."""
    subdirectory_count: int
    """Child folders that were traversed (skipped ones are not counted)."""
    categories: dict[str, int]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DirectoryRecord:
        return cls(**{**data, "categories": dict(data["categories"])})


@dataclass(frozen=True, slots=True)
class SkippedEntry:
    """An entry deliberately not inventoried, or one that could not be read.

    A sensitive filename is replaced by ``[redacted]`` in ``relative_path``.
    """

    root_id: str | None
    relative_path: str
    kind: EntryKind
    reason: SkipReason
    detail: str | None = None
    """Exception type for errors (e.g. ``"PermissionError"``), never its message."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkippedEntry:
        return cls(
            root_id=data["root_id"],
            relative_path=data["relative_path"],
            kind=EntryKind(data["kind"]),
            reason=SkipReason(data["reason"]),
            detail=data.get("detail"),
        )


@dataclass(frozen=True, slots=True)
class InventorySummary:
    total_files: int
    total_size_bytes: int
    directories_scanned: int
    skipped_entries: int
    """All skipped entries, including errors."""
    skipped_by_reason: dict[str, int]
    errors: int
    errors_by_type: dict[str, int]
    files_by_category: dict[str, int]
    files_by_extension: dict[str, int]
    files_by_language: dict[str, int]
    unrecognized_files: int
    images_included: bool
    excluded_image_files: int
    """Image files seen but not inventoried because images were disabled."""
    cloud_only_files_skipped: int
    """Cloud-only placeholder files seen but not inventoried (never opened)."""
    per_root: dict[str, dict[str, int]]
    """Root id → counts of files, directories, skipped entries and errors."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InventorySummary:
        return cls(**data)


@dataclass(frozen=True, slots=True)
class ScanReport:
    """Metadata and summary for one scan."""

    schema_version: int
    mode: ScanMode
    roots: tuple[ScanRoot, ...]
    started_at: datetime
    finished_at: datetime
    complete: bool
    """False if the scan stopped early or any folder was skipped for a limit."""
    stop_reason: str | None
    """Why the scan stopped early: max_files, max_directories, time_budget,
    cancelled or interrupted. None if it ran to the end."""
    limits_reached: tuple[str, ...]
    """Limits that caused folders to be skipped without stopping (max_depth)."""
    limits: ResourceLimits
    unrecognized_extensions: dict[str, int]
    """Counts of files whose extension is not in the registry; ``"(none)"`` for no extension."""
    summary: InventorySummary

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScanReport:
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported inventory schema version: {version!r}")
        return cls(
            schema_version=version,
            mode=ScanMode(data["mode"]),
            roots=tuple(ScanRoot.from_dict(r) for r in data["roots"]),
            started_at=datetime.fromisoformat(data["started_at"]),
            finished_at=datetime.fromisoformat(data["finished_at"]),
            complete=data["complete"],
            stop_reason=data["stop_reason"],
            limits_reached=tuple(data["limits_reached"]),
            limits=ResourceLimits.from_dict(data["limits"]),
            unrecognized_extensions=dict(data["unrecognized_extensions"]),
            summary=InventorySummary.from_dict(data["summary"]),
        )


@dataclass(frozen=True, slots=True)
class Inventory:
    """A complete scan result held in memory."""

    report: ScanReport
    files: tuple[FileRecord, ...]
    directories: tuple[DirectoryRecord, ...]
    skipped: tuple[SkippedEntry, ...]

    @property
    def summary(self) -> InventorySummary:
        return self.report.summary

    @property
    def complete(self) -> bool:
        return self.report.complete

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Inventory:
        return cls(
            report=ScanReport.from_dict(data["report"]),
            files=tuple(FileRecord.from_dict(f) for f in data["files"]),
            directories=tuple(DirectoryRecord.from_dict(d) for d in data["directories"]),
            skipped=tuple(SkippedEntry.from_dict(s) for s in data["skipped"]),
        )

    @classmethod
    def from_json(cls, text: str) -> Inventory:
        return cls.from_dict(json.loads(text))


def _parse_datetime(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def to_jsonable(value: Any) -> Any:
    """Convert models (and anything they contain) to JSON-safe values."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (list, tuple, frozenset, set)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    return value
