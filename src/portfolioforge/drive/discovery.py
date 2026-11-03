"""Turning Drive metadata into an ordinary inventory. Nothing here can download a file.

The same rules as the local scanner apply: credential locations, private folders, sensitive names,
links, caches, environments and build output are skipped; files of unknown types are only counted.
Files you don't own are left out unless asked for. Drive allows names no file system does, so every
name is made safe before it becomes a path. The ``absolute_path`` of each record is where the file
*would* be cached, and nothing exists there until you approve downloading it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterable

from ..inventory import rules
from ..inventory.config import ResourceLimits
from ..inventory.formats import Category, lookup
from ..inventory.models import (
    SCHEMA_VERSION,
    DirectoryRecord,
    EntryKind,
    FileRecord,
    InventorySummary,
    RootOrigin,
    ScanMode,
    ScanPlan,
    ScanReport,
    ScanRoot,
    SkippedEntry,
    SkipReason,
)
from ..inventory.scanner import _file_id, _sorted_counts
from ..inventory.sinks import DRIVE_FILES_NAME, InventorySink
from ..paths import drive_cache_dir, inventories_dir
from ..privacy import DEFAULT_PRIVATE_FOLDER_NAMES, PRIVATE_PLACEHOLDER
from .auth import DriveError
from .client import (
    COLAB_MIME,
    FOLDER_MIME,
    NATIVE_MIME_PREFIX,
    NOTEBOOK_MIMES,
    SHORTCUT_MIME,
    DriveClient,
    DriveEntry,
    is_drive_id,
    quote_query,
)

ROOT_LABEL = "Google Drive"
_NOTEBOOK_EXTENSION = ".ipynb"
_MAX_NAME_LENGTH = 120
_UNSAFE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
_RESERVED = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))})


@dataclass(frozen=True)
class DriveOptions:
    folders: tuple[str, ...] = ()
    """Names of top-level folders, or folder IDs. Empty means all of My Drive."""
    colab_only: bool = False
    include_shared: bool = False
    include_images: bool = False
    skip_build_dirs: bool = True
    exclude_names: frozenset[str] = frozenset()
    private_names: frozenset[str] = DEFAULT_PRIVATE_FOLDER_NAMES
    limits: ResourceLimits = field(default_factory=ResourceLimits)


@dataclass(frozen=True)
class DriveFileRef:
    """How to fetch one inventoried file after approval. Kept beside the inventory, on this computer."""

    file_record_id: str
    drive_id: str
    md5: str | None
    mime_type: str


@dataclass(frozen=True)
class Target:
    drive_id: str
    root: ScanRoot


@dataclass
class Listing:
    targets: tuple[Target, ...]
    entries: dict[str, DriveEntry]
    children: dict[str, list[DriveEntry]]
    whole_drive: bool
    stop_reason: str | None = None


@dataclass(frozen=True)
class Discovery:
    report: ScanReport
    refs: tuple[DriveFileRef, ...]


def new_inventory_key(now: datetime | None = None) -> str:
    return "drive-" + (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


def safe_name(raw: str) -> str:
    """One path component that is valid and harmless everywhere. Never contains a separator or ``..``."""
    name = _UNSAFE.sub("_", unicodedata.normalize("NFC", raw)).strip().rstrip(". ")
    if not name:
        return "_"
    stem, extension = os.path.splitext(name)
    if name.split(".")[0].casefold() in _RESERVED:
        name, stem = "_" + name, "_" + stem
    if len(name) > _MAX_NAME_LENGTH:
        extension = extension[:20]
        name = stem[:_MAX_NAME_LENGTH - len(extension)].rstrip(". ") + extension
    return name


def is_notebook(entry: DriveEntry) -> bool:
    return entry.mime_type in NOTEBOOK_MIMES or entry.name.casefold().endswith(_NOTEBOOK_EXTENSION)


def _display_name(entry: DriveEntry) -> str:
    """Colab can hold a notebook whose title has no extension; its MIME type still says what it is."""
    if entry.mime_type == COLAB_MIME and not entry.name.casefold().endswith(_NOTEBOOK_EXTENSION):
        return entry.name + _NOTEBOOK_EXTENSION
    return entry.name


def _short(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _unique_names(entries: Iterable[DriveEntry]) -> list[tuple[DriveEntry, str]]:
    """Drive lets one folder hold several items with the same name. The order is stable across scans."""
    taken: set[str] = set()
    result = []
    for entry in sorted(entries, key=lambda e: e.id):
        name = safe_name(_display_name(entry))
        if name.casefold() in taken:
            stem, extension = os.path.splitext(name)
            name = f"{stem} (drive-{_short(entry.id, 6)}){extension}"
        taken.add(name.casefold())
        result.append((entry, name))
    return sorted(result, key=lambda pair: pair[1])


def _scan_root(drive_id: str, label: str) -> ScanRoot:
    root_id = "gd_" + _short(drive_id)
    return ScanRoot(root_id, f"gdrive://{root_id}", RootOrigin.GOOGLE_DRIVE, label)


def resolve_targets(client: DriveClient, options: DriveOptions) -> tuple[Target, ...]:
    root = client.get("root")
    if not options.folders:
        return (Target(root.id, _scan_root(root.id, ROOT_LABEL)),)
    targets: dict[str, Target] = {}
    for wanted in options.folders:
        query = (f"name = {quote_query(wanted)} and {quote_query(root.id)} in parents "
                 f"and mimeType = {quote_query(FOLDER_MIME)} and trashed = false")
        matches = list(client.list(query))
        if len(matches) > 1:
            raise DriveError("Several top-level folders have that name. Use the folder's ID from its Drive address.")
        if matches:
            folder = matches[0]
        elif is_drive_id(wanted):
            folder = client.get(wanted)
        else:
            raise DriveError("No top-level folder has that name. Give its exact name, or its ID from its Drive address.")
        if not folder.is_folder:
            raise DriveError("That Drive item is not a folder.")
        name = safe_name(folder.name)
        if rules.is_sensitive_name(name) or name.casefold() in options.private_names \
                or name.casefold() in rules.CREDENTIAL_DIR_NAMES:
            raise DriveError("That folder is private or sensitive, so it can't be scanned.")
        if not folder.owned_by_me and not options.include_shared:
            raise DriveError("You don't own that folder. Add --include-shared if you mean to scan it.")
        targets[folder.id] = Target(folder.id, _scan_root(folder.id, f"{ROOT_LABEL}/{name}"))
    return tuple(targets.values())


def collect(client: DriveClient, options: DriveOptions, targets: tuple[Target, ...],
            *, clock: Callable[[], float] = time.monotonic) -> Listing:
    """Fetch metadata. All of it is fetched before anything is written, so a failure leaves no partial inventory."""
    whole = not options.folders
    start = clock()
    limits = options.limits
    ceiling = None if limits.max_files is None or limits.max_directories is None else limits.max_files + limits.max_directories
    entries: dict[str, DriveEntry] = {}
    stop: list[str] = []

    def exhausted() -> bool:
        if limits.time_budget_seconds is not None and clock() - start >= limits.time_budget_seconds:
            stop.append("time_budget")
        elif ceiling is not None and len(entries) >= ceiling:
            stop.append("max_files")
        return bool(stop)

    if whole:
        query = "trashed = false" if options.include_shared else "trashed = false and 'me' in owners"
        for entry in client.list(query):
            if exhausted():
                break
            entries[entry.id] = entry
    else:
        # Folder by folder, so what you didn't ask for is never listed, nor is inside a private folder.
        pending = [t.drive_id for t in targets]
        seen = set(pending)
        while pending and not stop:
            for entry in client.list(f"{quote_query(pending.pop())} in parents and trashed = false"):
                if exhausted():
                    break
                entries[entry.id] = entry
                if entry.is_folder and entry.id not in seen and _folder_name_reason(safe_name(entry.name), options) is None \
                        and (entry.owned_by_me or options.include_shared):
                    seen.add(entry.id)
                    pending.append(entry.id)
    children: dict[str, list[DriveEntry]] = defaultdict(list)
    roots = {t.drive_id for t in targets}
    for entry in entries.values():
        if entry.id not in roots:  # My Drive's own folder can appear in a listing; it is never its own child
            for parent in entry.parents or ("",):
                children[parent].append(entry)
    return Listing(targets, entries, children, whole, stop[0] if stop else None)


def _folder_name_reason(name: str, options: DriveOptions) -> SkipReason | None:
    """The local scanner's folder rules that depend only on a name, in the same order."""
    folded = name.casefold()
    if folded in rules.CREDENTIAL_DIR_NAMES:
        return SkipReason.CREDENTIAL_LOCATION
    if folded in options.private_names:
        return SkipReason.PRIVATE_FOLDER
    if folded in options.exclude_names:
        return SkipReason.USER_EXCLUDED
    if name.startswith("."):
        return SkipReason.HIDDEN
    if folded in rules.JUNK_DIR_NAMES:
        return SkipReason.IGNORED_NAME
    if folded in rules.VIRTUAL_ENV_DIR_NAMES:
        return SkipReason.VIRTUAL_ENV
    if options.skip_build_dirs and rules.is_build_dir_name(folded):
        return SkipReason.BUILD_DIR
    if folded in rules.INSTALLED_TOOL_DIR_NAMES:
        return SkipReason.INSTALLED_TOOL
    return None


def _listing_reason(names: set[str]) -> SkipReason | None:
    if rules.is_virtual_env_listing(names):
        return SkipReason.VIRTUAL_ENV
    if rules.is_installed_tool_listing(names):
        return SkipReason.INSTALLED_TOOL
    if rules.is_installed_r_package_listing(names):
        return SkipReason.R_PACKAGE_LIBRARY
    return None


class _StopWalk(Exception):
    pass


class _Builder:
    def __init__(self, listing: Listing, options: DriveOptions, key: str, sink: InventorySink) -> None:
        self.listing, self.options, self.key, self.sink = listing, options, key, sink
        self.refs: list[DriveFileRef] = []
        self.visited: set[str] = set()
        self.files = self.dirs = self.skipped = self.size = self.images = self.native = 0
        self.by_category: Counter[str] = Counter()
        self.by_extension: Counter[str] = Counter()
        self.by_language: Counter[str] = Counter()
        self.by_reason: Counter[str] = Counter()
        self.unrecognized: Counter[str] = Counter()
        self.limits_reached: set[str] = set()
        self.stop_reason = listing.stop_reason
        self.per_root: dict[str, Counter[str]] = {
            t.root.id: Counter({"files": 0, "directories": 0, "skipped": 0, "errors": 0}) for t in listing.targets
        }
        self.has_notebook = self._folders_with_notebooks() if options.colab_only else None

    def _folders_with_notebooks(self) -> set[str]:
        """Colab-only scans record just the folders on the way to a notebook."""
        found: set[str] = set()
        for entry in self.listing.entries.values():
            if entry.is_folder or not is_notebook(entry):
                continue
            pending = list(entry.parents)
            while pending:
                parent = pending.pop()
                if parent not in found:
                    found.add(parent)
                    above = self.listing.entries.get(parent)
                    pending.extend(above.parents if above else ())
        return found

    def _skip(self, root: ScanRoot, path: str, kind: EntryKind, reason: SkipReason, detail: str | None = None) -> None:
        self.sink.add_skipped(SkippedEntry(root.id, path, kind, reason, detail))
        self.skipped += 1
        self.by_reason[reason.value] += 1
        self.per_root[root.id]["skipped"] += 1

    def run(self) -> None:
        try:
            for target in self.listing.targets:
                self._walk(target.root, target.drive_id, (), self._outside_my_drive(target))
        except _StopWalk:
            pass

    def _outside_my_drive(self, target: Target) -> list[str]:
        """Parents that aren't visible, such as someone else's folder holding one of your files."""
        if not self.listing.whole_drive:
            return []
        known = self.listing.entries.keys() | {target.drive_id}
        return sorted(p for p in self.listing.children if p not in known
                      and (self.has_notebook is None or p in self.has_notebook or not p))

    def _walk(self, root: ScanRoot, folder_id: str, parts: tuple[str, ...], outside: Iterable[str] = ()) -> bool:
        """Record one folder and what's below it. Returns False if it was skipped as a whole.

        Items sharing an unseen parent stay together, in a folder named after no real one.
        """
        rel = "/".join(parts)
        if folder_id in self.visited:
            self._skip(root, rel, EntryKind.DIRECTORY, SkipReason.ALREADY_SCANNED)
            return False
        self.visited.add(folder_id)
        kids = _unique_names(self.listing.children.get(folder_id, ()))
        if parts:
            reason = _listing_reason({name.casefold() for _, name in kids})
            if reason is not None:
                self._skip(root, rel, EntryKind.DIRECTORY, reason)
                return False
        limit = self.options.limits.max_directories
        if limit is not None and self.dirs >= limit:
            self.stop_reason = "max_directories"
            raise _StopWalk
        self.dirs += 1
        self.per_root[root.id]["directories"] += 1

        record = _Folder()
        try:
            for entry, name in kids:
                if entry.is_folder:
                    self._folder_child(root, parts, entry, name, record)
                else:
                    self._file_child(root, parts, entry, name, record)
            for parent in outside:
                if self._walk(root, parent, (*parts, f"Other location {_short(parent or 'none', 6)}")):
                    record.subdirectories += 1
        finally:
            self.sink.add_directory(DirectoryRecord(
                root_id=root.id, relative_path=rel, absolute_path=f"{root.path}/{rel}" if rel else root.path,
                name=parts[-1] if parts else root.label, parent_dir="/".join(parts[:-1]) if parts else None,
                depth=len(parts), file_count=record.files, subdirectory_count=record.subdirectories,
                categories=_sorted_counts(record.categories),
                marker_files=tuple(sorted(record.marker_files, key=str.casefold)),
                marker_dirs=tuple(sorted(record.marker_dirs, key=str.casefold)),
                unrecognized_extensions=_sorted_counts(record.unrecognized),
            ))
        return True

    def _folder_child(self, root: ScanRoot, parts: tuple[str, ...], entry: DriveEntry, name: str, parent: _Folder) -> None:
        if self.has_notebook is not None and entry.id not in self.has_notebook:
            return
        child = (*parts, name)
        display = "/".join(child)
        if not entry.owned_by_me and not self.options.include_shared:
            self._skip(root, "/".join((*parts, PRIVATE_PLACEHOLDER)), EntryKind.DIRECTORY, SkipReason.NOT_OWNED)
            return
        reason = _folder_name_reason(name, self.options)
        if reason is None and rules.is_r_library_dir(name.casefold(), parts[-1].casefold() if parts else ""):
            reason = SkipReason.R_PACKAGE_LIBRARY
        depth = self.options.limits.max_depth
        if reason is None and depth is not None and len(child) > depth:
            reason = SkipReason.MAX_DEPTH
            self.limits_reached.add("max_depth")
        if reason is SkipReason.PRIVATE_FOLDER:
            self._skip(root, "/".join((*parts, PRIVATE_PLACEHOLDER)), EntryKind.DIRECTORY, reason)
            return
        if rules.is_project_marker_dir(name):
            parent.marker_dirs.append(name)
        if reason is not None:
            self._skip(root, display, EntryKind.DIRECTORY, reason)
            return
        if self._walk(root, entry.id, child):
            parent.subdirectories += 1

    def _file_child(self, root: ScanRoot, parts: tuple[str, ...], entry: DriveEntry, name: str, parent: _Folder) -> None:
        if self.options.colab_only and not is_notebook(entry):
            return
        sensitive = rules.is_sensitive_name(name)
        display = "/".join((*parts, rules.REDACTED if sensitive else name))
        if not entry.owned_by_me and not self.options.include_shared:
            self._skip(root, "/".join((*parts, PRIVATE_PLACEHOLDER)), EntryKind.FILE, SkipReason.NOT_OWNED)
            return
        if entry.mime_type == SHORTCUT_MIME:
            self._skip(root, display, EntryKind.LINK, SkipReason.LINK)  # never followed, like a local link
            return
        if entry.mime_type.startswith(NATIVE_MIME_PREFIX):
            self.native += 1  # Docs, Sheets and Slides have no file to download
            return
        if not sensitive and rules.is_project_marker_file(name):
            parent.marker_files.append(name)
        if sensitive:
            self._skip(root, display, EntryKind.FILE, SkipReason.SENSITIVE_NAME)
            return
        if name.startswith("."):
            self._skip(root, display, EntryKind.FILE, SkipReason.HIDDEN)
            return
        if name.startswith(rules.OFFICE_LOCK_PREFIX):
            self._skip(root, display, EntryKind.FILE, SkipReason.OFFICE_LOCK_FILE)
            return
        if name.casefold() in rules.OS_METADATA_FILES:
            self._skip(root, display, EntryKind.FILE, SkipReason.OS_METADATA_FILE)
            return
        extension = os.path.splitext(name)[1].lower()
        file_format = lookup(extension) if extension else None
        if file_format is None:
            self.unrecognized[extension or "(none)"] += 1
            parent.unrecognized[extension or "(none)"] += 1
            return
        if file_format.category is Category.IMAGE and not self.options.include_images:
            self.images += 1
            return
        if entry.size is None:  # an approval relies on the size, so this one couldn't be read safely later
            self._skip(root, display, EntryKind.FILE, SkipReason.CLOUD_ONLY, "no_size")
            return
        limit = self.options.limits.max_files
        if limit is not None and self.files >= limit:
            self.stop_reason = "max_files"
            raise _StopWalk
        relative = "/".join((*parts, name))
        record_id = _file_id(root.id, relative)
        self.sink.add_file(FileRecord(
            id=record_id, root_id=root.id, relative_path=relative,
            absolute_path=str(drive_cache_dir() / self.key / _short(entry.id) / name),
            name=name, stem=os.path.splitext(name)[0], extension=extension, category=file_format.category,
            language=file_format.language, parent_dir="/".join(parts), path_parts=parts, depth=len(parts),
            size_bytes=entry.size, modified_at=entry.modified_at, created_at=entry.created_at, location_hint=root.label,
        ))
        self.refs.append(DriveFileRef(record_id, entry.id, entry.md5, entry.mime_type))
        self.files += 1
        self.size += entry.size
        self.by_category[file_format.category.value] += 1
        self.by_extension[extension] += 1
        if file_format.language:
            self.by_language[file_format.language] += 1
        self.per_root[root.id]["files"] += 1
        parent.files += 1
        parent.categories[file_format.category.value] += 1

    def report(self, started_at: datetime) -> ScanReport:
        summary = InventorySummary(
            total_files=self.files, total_size_bytes=self.size, directories_scanned=self.dirs,
            skipped_entries=self.skipped, skipped_by_reason=_sorted_counts(self.by_reason), errors=0, errors_by_type={},
            files_by_category=_sorted_counts(self.by_category), files_by_extension=_sorted_counts(self.by_extension),
            files_by_language=_sorted_counts(self.by_language), unrecognized_files=sum(self.unrecognized.values()),
            images_included=self.options.include_images, excluded_image_files=self.images,
            cloud_only_files_skipped=self.native,
            per_root={root_id: dict(sorted(c.items())) for root_id, c in self.per_root.items()},
        )
        return ScanReport(
            schema_version=SCHEMA_VERSION, mode=ScanMode.GOOGLE_DRIVE, roots=tuple(t.root for t in self.listing.targets),
            started_at=started_at, finished_at=datetime.now(UTC),
            complete=self.stop_reason is None and not self.limits_reached, stop_reason=self.stop_reason,
            limits_reached=tuple(sorted(self.limits_reached)), limits=self.options.limits,
            unrecognized_extensions=_sorted_counts(self.unrecognized), summary=summary,
        )


@dataclass
class _Folder:
    files: int = 0
    subdirectories: int = 0
    categories: Counter[str] = field(default_factory=Counter)
    marker_files: list[str] = field(default_factory=list)
    marker_dirs: list[str] = field(default_factory=list)
    unrecognized: Counter[str] = field(default_factory=Counter)


def build_inventory_records(listing: Listing, options: DriveOptions, key: str, sink: InventorySink,
                            started_at: datetime) -> Discovery:
    """Send records to ``sink``. The caller closes it, after saving the refs, since a report marks a finished inventory."""
    builder = _Builder(listing, options, key, sink)
    builder.run()
    return Discovery(builder.report(started_at), tuple(builder.refs))


def make_plan(targets: tuple[Target, ...], output_dir: str | os.PathLike[str]) -> ScanPlan:
    return ScanPlan(mode=ScanMode.GOOGLE_DRIVE, roots=tuple(t.root for t in targets), excluded=(), optional=(),
                    profile_dir=None, users_dirs=(), drive_roots=(), output_dir=str(output_dir))


def write_refs(output_dir: Path, refs: Iterable[DriveFileRef]) -> None:
    with open(output_dir / DRIVE_FILES_NAME, "w", encoding="utf-8", newline="\n") as handle:
        for ref in refs:
            handle.write(json.dumps(asdict(ref)) + "\n")


def load_refs(inventory_key: str) -> dict[str, DriveFileRef]:
    """Only ever looked up in the default location: Drive inventories can't be written anywhere else."""
    path = inventories_dir() / inventory_key / DRIVE_FILES_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise DriveError("This Drive inventory has no download records. Run the Drive scan again.") from None
    refs = (DriveFileRef(**json.loads(line)) for line in lines if line.strip())
    return {r.file_record_id: r for r in refs}
