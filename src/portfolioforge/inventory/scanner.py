"""Read-only, metadata-only directory walker.

The walker lists folders with ``os.scandir`` and reads only the metadata that
the listing returns (names, sizes, timestamps, attributes). It never opens,
reads, executes, modifies or uploads a file, never follows a link or junction,
and never enters a cloud-only placeholder folder.
"""

from __future__ import annotations

import hashlib
import os
import stat
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Callable, Iterable

from . import rules, selfsource
from .config import InventoryConfig, ResourceLimits
from .formats import Category, lookup
from .models import (
    ERROR_REASONS,
    SCHEMA_VERSION,
    DirectoryRecord,
    EntryKind,
    FileRecord,
    Inventory,
    InventorySummary,
    ScanMode,
    ScanPlan,
    ScanReport,
    ScanRoot,
    SkippedEntry,
    SkipReason,
)
from .platform import (
    CLOUD_DIR_ATTRIBUTES,
    CLOUD_FILE_ATTRIBUTES,
    FILE_ATTRIBUTE_HIDDEN,
    FILE_ATTRIBUTE_SYSTEM,
    SystemInfo,
    file_attributes,
    is_link_stat,
)
from .roots import is_within, normalize_path, plan_targeted
from .sinks import InventorySink, MemorySink

#: Returns Windows file attributes for an entry. Replaceable in tests.
AttributeReader = Callable[[str, os.stat_result], int]


class FullScanNotApproved(PermissionError):
    """Raised when a full-computer plan is run without explicit approval."""


@dataclass(frozen=True, slots=True)
class ScanProgress:
    """Periodic progress. Names a root, never an individual file or folder."""

    root_index: int
    root_count: int
    root_path: str
    directories_visited: int
    files_inventoried: int
    skipped_entries: int
    errors: int
    elapsed_seconds: float
    finished: bool = False


def run_scan(
    plan: ScanPlan,
    *,
    sink: InventorySink,
    config: InventoryConfig | None = None,
    limits: ResourceLimits | None = None,
    allow_full_computer: bool = False,
    on_progress: Callable[[ScanProgress], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    attributes: AttributeReader | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ScanReport:
    """Walk every root in ``plan``, sending records to ``sink``.

    A full-computer plan only runs with ``allow_full_computer=True``.
    """
    if plan.mode is ScanMode.FULL_COMPUTER and not allow_full_computer:
        raise FullScanNotApproved(
            "Full-computer scans must be explicitly approved (allow_full_computer=True)."
        )
    walker = _Walker(
        plan=plan,
        sink=sink,
        config=config or InventoryConfig(),
        limits=limits or ResourceLimits(),
        on_progress=on_progress,
        should_stop=should_stop,
        attributes=attributes or _default_attributes,
        clock=clock,
        sleep=sleep,
    )
    return walker.run()


def scan_inventory(
    paths: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
    *,
    config: InventoryConfig | None = None,
    limits: ResourceLimits | None = None,
    system: SystemInfo | None = None,
    on_progress: Callable[[ScanProgress], None] | None = None,
    attributes: AttributeReader | None = None,
) -> Inventory:
    """Targeted scan of one or more folders, returned as an in-memory Inventory."""
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    plan = plan_targeted(paths, system=system)
    sink = MemorySink()
    run_scan(
        plan, sink=sink, config=config, limits=limits,
        on_progress=on_progress, attributes=attributes,
    )
    return sink.inventory()


def _default_attributes(path: str, st: os.stat_result) -> int:
    return file_attributes(st)


class _StopScan(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class _DirState:
    root: ScanRoot
    abs_path: str
    parts: tuple[str, ...]
    file_count: int = 0
    subdirectory_count: int = 0
    categories: Counter[str] = field(default_factory=Counter)


_ENTER = "enter"
_EXIT = "exit"


class _Walker:
    def __init__(
        self,
        *,
        plan: ScanPlan,
        sink: InventorySink,
        config: InventoryConfig,
        limits: ResourceLimits,
        on_progress: Callable[[ScanProgress], None] | None,
        should_stop: Callable[[], bool] | None,
        attributes: AttributeReader,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._plan = plan
        self._sink = sink
        self._config = config
        self._limits = limits
        self._on_progress = on_progress
        self._should_stop = should_stop
        self._attributes = attributes
        self._clock = clock
        self._sleep = sleep

        self._profile = normalize_path(plan.profile_dir) if plan.profile_dir else None
        self._users_dirs = frozenset(normalize_path(p) for p in plan.users_dirs)
        self._drive_roots = frozenset(normalize_path(p) for p in plan.drive_roots)
        self._root_paths = frozenset(normalize_path(r.path) for r in plan.roots)
        self._output_dir = normalize_path(plan.output_dir) if plan.output_dir else None
        self._exclude_paths = tuple(normalize_path(p) for p in config.exclude_paths)
        self._exclude_names = frozenset(n.casefold() for n in config.exclude_dir_names)
        self._include_names = frozenset(n.casefold() for n in config.include_dir_names)
        source = selfsource.portfolioforge_source_dir() if config.skip_portfolioforge_source else None
        self._self_source = normalize_path(source) if source else None

        self._visited: set[tuple[int, int]] = set()
        self._stack: list[tuple] = []
        self._root: ScanRoot | None = None
        self._root_index = 0
        self._start = 0.0
        self._last_progress = 0.0

        self._files = 0
        self._dirs = 0
        self._skipped = 0
        self._errors = 0
        self._entries = 0
        self._size = 0
        self._by_category: Counter[str] = Counter()
        self._by_extension: Counter[str] = Counter()
        self._by_language: Counter[str] = Counter()
        self._skipped_by_reason: Counter[str] = Counter()
        self._errors_by_type: Counter[str] = Counter()
        self._unrecognized: Counter[str] = Counter()
        self._excluded_images = 0
        self._cloud_files = 0
        self._limits_reached: set[str] = set()
        self._per_root = {
            r.id: Counter({"files": 0, "directories": 0, "skipped": 0, "errors": 0})
            for r in plan.roots
        }

    # --- main loop ---------------------------------------------------------------

    def run(self) -> ScanReport:
        started_at = datetime.now(UTC)
        self._start = self._last_progress = self._clock()
        stop_reason: str | None = None
        try:
            for index, root in enumerate(self._plan.roots, start=1):
                self._root, self._root_index = root, index
                self._walk_root(root)
        except _StopScan as stop:
            stop_reason = stop.reason
        except KeyboardInterrupt:
            stop_reason = "interrupted"
        finally:
            self._flush_pending()
        self._emit_progress(finished=True)
        report = ScanReport(
            schema_version=SCHEMA_VERSION,
            mode=self._plan.mode,
            roots=self._plan.roots,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            complete=stop_reason is None and not self._limits_reached,
            stop_reason=stop_reason,
            limits_reached=tuple(sorted(self._limits_reached)),
            limits=self._limits,
            unrecognized_extensions=_sorted_counts(self._unrecognized),
            summary=self._summary(),
        )
        self._sink.close(report)
        return report

    def _walk_root(self, root: ScanRoot) -> None:
        self._stack = [(_ENTER, root.path, (), None)]
        while self._stack:
            frame = self._stack.pop()
            if frame[0] == _EXIT:
                self._emit_directory(frame[1])
                continue
            self._check_limits()
            self._enter(frame[1], frame[2], frame[3])

    def _flush_pending(self) -> None:
        """On an early stop, still emit records for folders already entered."""
        for frame in reversed(self._stack):
            if frame[0] == _EXIT:
                self._emit_directory(frame[1])
        self._stack = []

    def _check_limits(self) -> None:
        now = self._clock()
        if self._should_stop is not None and self._should_stop():
            raise _StopScan("cancelled")
        budget = self._limits.time_budget_seconds
        if budget is not None and now - self._start >= budget:
            raise _StopScan("time_budget")
        max_dirs = self._limits.max_directories
        if max_dirs is not None and self._dirs >= max_dirs:
            raise _StopScan("max_directories")
        if now - self._last_progress >= self._limits.progress_interval_seconds:
            self._last_progress = now
            self._emit_progress(finished=False, now=now)

    # --- folders -----------------------------------------------------------------

    def _enter(self, abs_path: str, parts: tuple[str, ...], parent: _DirState | None) -> None:
        root = self._root
        assert root is not None
        rel = "/".join(parts)
        os_path = _os_path(abs_path)
        try:
            st = os.stat(os_path, follow_symlinks=False)
        except OSError as exc:
            self._error(root, rel, EntryKind.DIRECTORY, exc)
            _undo_subdirectory(parent)
            return
        identity = (st.st_dev, st.st_ino)
        if st.st_ino:
            if identity in self._visited:
                self._skip(root, rel, EntryKind.DIRECTORY, SkipReason.ALREADY_SCANNED)
                _undo_subdirectory(parent)
                return
            self._visited.add(identity)
        try:
            with os.scandir(os_path) as iterator:
                entries = sorted(iterator, key=lambda e: e.name)
        except OSError as exc:
            self._error(root, rel, EntryKind.DIRECTORY, exc)
            _undo_subdirectory(parent)
            return
        if parts and parts[-1].casefold() not in self._include_names:
            names = {e.name.casefold() for e in entries}
            reason = None
            if rules.is_virtual_env_listing(names):
                reason = SkipReason.VIRTUAL_ENV
            elif self._config.skip_installed_tools and rules.is_installed_tool_listing(names):
                reason = SkipReason.INSTALLED_TOOL
            elif self._config.skip_r_libraries and rules.is_installed_r_package_listing(names):
                reason = SkipReason.R_PACKAGE_LIBRARY
            elif self._config.skip_portfolioforge_source and selfsource.looks_like_portfolioforge_checkout(
                abs_path, names
            ):
                reason = SkipReason.PORTFOLIOFORGE_SOURCE
            if reason is not None:
                self._skip(root, rel, EntryKind.DIRECTORY, reason)
                _undo_subdirectory(parent)
                return

        state = _DirState(root=root, abs_path=abs_path, parts=parts)
        self._dirs += 1
        self._per_root[root.id]["directories"] += 1
        self._stack.append((_EXIT, state))
        children: list[tuple[str, tuple[str, ...]]] = []
        for entry in entries:
            self._process_entry(state, entry, children)
        for child_path, child_parts in reversed(children):
            self._stack.append((_ENTER, child_path, child_parts, state))

    def _dir_skip_reason(self, parent: _DirState, name: str, path: str, attrs: int) -> SkipReason | None:
        folded = name.casefold()
        # protected tier
        if folded in rules.CREDENTIAL_DIR_NAMES:
            return SkipReason.CREDENTIAL_LOCATION
        parent_norm = normalize_path(parent.abs_path)
        at_drive_top = parent_norm in self._drive_roots or os.path.dirname(parent_norm) == parent_norm
        if at_drive_top and folded in rules.PROTECTED_TOP_LEVEL_DIRS:
            return SkipReason.PROTECTED_LOCATION
        child_norm = normalize_path(path)
        if (
            parent_norm in self._users_dirs
            and child_norm != self._profile
            and child_norm not in self._root_paths
        ):
            return SkipReason.OTHER_PROFILE
        if self._output_dir and is_within(child_norm, self._output_dir):
            return SkipReason.OUTPUT_DIR
        if attrs & CLOUD_DIR_ATTRIBUTES:
            return SkipReason.CLOUD_ONLY
        # user settings
        if folded in self._exclude_names or any(is_within(child_norm, p) for p in self._exclude_paths):
            return SkipReason.USER_EXCLUDED
        if self._self_source and child_norm == self._self_source:
            return SkipReason.PORTFOLIOFORGE_SOURCE
        # default tier
        if folded in self._include_names:
            return None
        if self._config.skip_r_libraries:
            parent_name = parent.parts[-1] if parent.parts else os.path.basename(parent_norm)
            if rules.is_r_library_dir(folded, parent_name.casefold()):
                return SkipReason.R_PACKAGE_LIBRARY
        if name.startswith(".") or attrs & FILE_ATTRIBUTE_HIDDEN:
            return SkipReason.HIDDEN
        if attrs & FILE_ATTRIBUTE_SYSTEM:
            return SkipReason.SYSTEM
        if folded in rules.JUNK_DIR_NAMES:
            return SkipReason.IGNORED_NAME
        if folded in rules.VIRTUAL_ENV_DIR_NAMES:
            return SkipReason.VIRTUAL_ENV
        if self._config.skip_build_dirs and rules.is_build_dir_name(folded):
            return SkipReason.BUILD_DIR
        if self._config.skip_installed_tools and folded in rules.INSTALLED_TOOL_DIR_NAMES:
            return SkipReason.INSTALLED_TOOL
        return None

    def _emit_directory(self, state: _DirState) -> None:
        parts = state.parts
        self._sink.add_directory(
            DirectoryRecord(
                root_id=state.root.id,
                relative_path="/".join(parts),
                absolute_path=state.abs_path,
                name=parts[-1] if parts else (os.path.basename(state.abs_path.rstrip("\\/")) or state.abs_path),
                parent_dir="/".join(parts[:-1]) if parts else None,
                depth=len(parts),
                file_count=state.file_count,
                subdirectory_count=state.subdirectory_count,
                categories=_sorted_counts(state.categories),
            )
        )

    # --- entries -----------------------------------------------------------------

    def _process_entry(
        self,
        parent: _DirState,
        entry: os.DirEntry[str],
        children: list[tuple[str, tuple[str, ...]]],
    ) -> None:
        root = parent.root
        name = entry.name
        child_parts = (*parent.parts, name)
        child_path = os.path.join(parent.abs_path, name)
        sensitive = rules.is_sensitive_name(name, self._config.extra_sensitive_patterns)
        display = "/".join((*parent.parts, rules.REDACTED if sensitive else name))
        self._throttle()
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError as exc:
            self._error(root, display, EntryKind.FILE, exc)
            return
        if is_link_stat(st):
            self._skip(root, display, EntryKind.LINK, SkipReason.LINK)
            return
        attrs = self._attributes(child_path, st)

        if stat.S_ISDIR(st.st_mode):
            reason = self._dir_skip_reason(parent, name, child_path, attrs)
            if reason is None and self._limits.max_depth is not None and len(child_parts) > self._limits.max_depth:
                reason = SkipReason.MAX_DEPTH
                self._limits_reached.add("max_depth")
            if reason is not None:
                self._skip(root, display, EntryKind.DIRECTORY, reason)
                return
            parent.subdirectory_count += 1
            children.append((child_path, child_parts))
            return

        if not stat.S_ISREG(st.st_mode):
            return  # devices, sockets and other special files are ignored
        reason = self._file_skip_reason(name, attrs, sensitive)
        if reason is SkipReason.CLOUD_ONLY:
            self._cloud_files += 1
            return
        if reason is not None:
            self._skip(root, display, EntryKind.FILE, reason)
            return

        extension = os.path.splitext(name)[1].lower()
        if extension == ".":
            extension = ""
        file_format = lookup(extension) if extension else None
        if file_format is None:
            self._unrecognized[extension or "(none)"] += 1
            return
        if file_format.category is Category.IMAGE and not self._config.include_images:
            self._excluded_images += 1
            return
        max_files = self._limits.max_files
        if max_files is not None and self._files >= max_files:
            raise _StopScan("max_files")

        relative_path = "/".join(child_parts)
        record = FileRecord(
            id=_file_id(root.id, relative_path),
            root_id=root.id,
            relative_path=relative_path,
            absolute_path=child_path,
            name=name,
            stem=os.path.splitext(name)[0],
            extension=extension,
            category=file_format.category,
            language=file_format.language,
            parent_dir="/".join(parent.parts),
            path_parts=parent.parts,
            depth=len(parent.parts),
            size_bytes=st.st_size,
            modified_at=_timestamp(st.st_mtime),
            created_at=_timestamp(getattr(st, "st_birthtime", None)),
            location_hint=root.label,
        )
        self._sink.add_file(record)
        self._files += 1
        self._size += st.st_size
        self._by_category[file_format.category.value] += 1
        self._by_extension[extension] += 1
        if file_format.language:
            self._by_language[file_format.language] += 1
        self._per_root[root.id]["files"] += 1
        parent.file_count += 1
        parent.categories[file_format.category.value] += 1

    def _file_skip_reason(self, name: str, attrs: int, sensitive: bool) -> SkipReason | None:
        if sensitive:
            return SkipReason.SENSITIVE_NAME
        if attrs & CLOUD_FILE_ATTRIBUTES:
            return SkipReason.CLOUD_ONLY
        if name.startswith(".") or attrs & FILE_ATTRIBUTE_HIDDEN:
            return SkipReason.HIDDEN
        if attrs & FILE_ATTRIBUTE_SYSTEM:
            return SkipReason.SYSTEM
        if name.startswith(rules.OFFICE_LOCK_PREFIX):
            return SkipReason.OFFICE_LOCK_FILE
        if name.casefold() in rules.OS_METADATA_FILES:
            return SkipReason.OS_METADATA_FILE
        return None

    # --- bookkeeping -------------------------------------------------------------

    def _skip(
        self,
        root: ScanRoot,
        relative_path: str,
        kind: EntryKind,
        reason: SkipReason,
        detail: str | None = None,
    ) -> None:
        self._sink.add_skipped(SkippedEntry(root.id, relative_path, kind, reason, detail))
        self._skipped += 1
        self._skipped_by_reason[reason.value] += 1
        self._per_root[root.id]["skipped"] += 1
        if reason in ERROR_REASONS:
            self._errors += 1
            self._errors_by_type[detail or "OSError"] += 1
            self._per_root[root.id]["errors"] += 1

    def _error(self, root: ScanRoot, relative_path: str, kind: EntryKind, exc: OSError) -> None:
        reason = SkipReason.PERMISSION_ERROR if isinstance(exc, PermissionError) else SkipReason.OS_ERROR
        self._skip(root, relative_path, kind, reason, type(exc).__name__)

    def _throttle(self) -> None:
        self._entries += 1
        limits = self._limits
        if limits.throttle_seconds > 0 and self._entries % max(limits.throttle_every, 1) == 0:
            self._sleep(limits.throttle_seconds)

    def _emit_progress(self, *, finished: bool, now: float | None = None) -> None:
        if self._on_progress is None:
            return
        now = self._clock() if now is None else now
        root = self._root
        self._on_progress(
            ScanProgress(
                root_index=self._root_index,
                root_count=len(self._plan.roots),
                root_path=root.path if root else "",
                directories_visited=self._dirs,
                files_inventoried=self._files,
                skipped_entries=self._skipped,
                errors=self._errors,
                elapsed_seconds=max(now - self._start, 0.0),
                finished=finished,
            )
        )

    def _summary(self) -> InventorySummary:
        return InventorySummary(
            total_files=self._files,
            total_size_bytes=self._size,
            directories_scanned=self._dirs,
            skipped_entries=self._skipped,
            skipped_by_reason=_sorted_counts(self._skipped_by_reason),
            errors=self._errors,
            errors_by_type=_sorted_counts(self._errors_by_type),
            files_by_category=_sorted_counts(self._by_category),
            files_by_extension=_sorted_counts(self._by_extension),
            files_by_language=_sorted_counts(self._by_language),
            unrecognized_files=sum(self._unrecognized.values()),
            images_included=self._config.include_images,
            excluded_image_files=self._excluded_images,
            cloud_only_files_skipped=self._cloud_files,
            per_root={root_id: dict(sorted(c.items())) for root_id, c in self._per_root.items()},
        )


def _undo_subdirectory(parent: _DirState | None) -> None:
    if parent is not None:
        parent.subdirectory_count -= 1


def _sorted_counts(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items()))


def _file_id(root_id: str, relative_path: str) -> str:
    digest = hashlib.sha256(f"{root_id}\0{relative_path}".encode("utf-8", "surrogatepass"))
    return "f_" + digest.hexdigest()[:16]


def _timestamp(value: float | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _os_path(path: str) -> str:
    """Use Windows extended-length syntax for long paths so deep folders still list."""
    if os.name == "nt" and len(path) >= 240 and not path.startswith("\\\\?\\"):
        if path.startswith("\\\\"):
            return "\\\\?\\UNC\\" + path[2:]
        return "\\\\?\\" + path
    return path
