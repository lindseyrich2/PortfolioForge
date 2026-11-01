"""Synthetic inventories for grouping tests. No real files, paths or names."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from portfolioforge.inventory import (
    DirectoryRecord,
    EntryKind,
    FileRecord,
    Inventory,
    InventorySummary,
    ResourceLimits,
    RootOrigin,
    ScanMode,
    ScanReport,
    ScanRoot,
    SkippedEntry,
    SkipReason,
    lookup,
)
from portfolioforge.inventory.sinks import build_inventory

BASE_TIME = datetime(2021, 9, 1, 12, 0, tzinfo=UTC)


def day(n: float) -> datetime:
    return BASE_TIME + timedelta(days=n)


def make_inventory(
    files: dict[str, Any],
    *,
    roots: dict[str, str] | None = None,
    marker_files: dict[str, list[str]] | None = None,
    marker_dirs: dict[str, list[str]] | None = None,
    skipped_dirs: dict[str, str] | None = None,
    schema_version: int = 2,
) -> Inventory:
    """Build an Inventory from ``{"<root>/<relative path>": spec}``.

    ``spec`` is a day offset (modification time = BASE_TIME + days), or a dict
    with ``day`` and/or ``size``. Paths without a root prefix use root "home".
    ``marker_files`` / ``marker_dirs`` / ``skipped_dirs`` are keyed by
    "<root>/<folder>" ("<root>" for the root itself).
    """
    roots = roots or {"home": "Synthetic Home"}
    root_ids = {name: f"r_{name}" for name in roots}
    records: list[FileRecord] = []
    folders: dict[tuple[str, str], dict[str, Any]] = {}

    def split(key: str) -> tuple[str, str]:
        head, _, rest = key.partition("/")
        return (head, rest) if head in roots else ("home", key)

    def ensure(root: str, rel: str) -> None:
        while True:
            folders.setdefault((root, rel), {"files": 0})
            if rel == "":
                return
            rel = rel.rpartition("/")[0]

    for key, spec in sorted(files.items()):
        root, rel = split(key)
        spec = {"day": spec} if isinstance(spec, (int, float)) else dict(spec)
        name = rel.rsplit("/", 1)[-1]
        parent = rel.rpartition("/")[0]
        stem, ext = os.path.splitext(name)
        fmt = lookup(ext.lower())
        assert fmt is not None, f"synthetic file needs a recognized extension: {name}"
        parts = tuple(parent.split("/")) if parent else ()
        ensure(root, parent)
        folders[(root, parent)]["files"] += 1
        records.append(FileRecord(
            id="f_" + hashlib.sha256(f"{root}/{rel}".encode()).hexdigest()[:16],
            root_id=root_ids[root], relative_path=rel, absolute_path=f"/synthetic/{root}/{rel}",
            name=name, stem=stem, extension=ext.lower(), category=fmt.category, language=fmt.language,
            parent_dir=parent, path_parts=parts, depth=len(parts), size_bytes=int(spec.get("size", 1000)),
            modified_at=day(spec.get("day", 0)), created_at=None, location_hint=roots[root],
        ))
    for key in [*(marker_files or {}), *(marker_dirs or {}), *(skipped_dirs or {})]:
        ensure(*split(key + "/x")[0:1], split(key + "/x")[1].rpartition("/")[0]) if key not in roots \
            else ensure(key, "")
    directories = []
    for (root, rel), info in sorted(folders.items()):
        mkey = f"{root}/{rel}" if rel else root
        directories.append(DirectoryRecord(
            root_id=root_ids[root], relative_path=rel, absolute_path=f"/synthetic/{root}/{rel}",
            name=rel.rsplit("/", 1)[-1] if rel else roots[root],
            parent_dir=(rel.rpartition("/")[0] if rel else None), depth=rel.count("/") + 1 if rel else 0,
            file_count=info["files"], subdirectory_count=0, categories={},
            marker_files=tuple((marker_files or {}).get(mkey, ())) if schema_version >= 2 else (),
            marker_dirs=tuple((marker_dirs or {}).get(mkey, ())) if schema_version >= 2 else (),
        ))
    skipped = []
    for key, reason in (skipped_dirs or {}).items():
        root, rel = split(key)
        skipped.append(SkippedEntry(root_ids[root], rel, EntryKind.DIRECTORY, SkipReason(reason)))
    report = ScanReport(
        schema_version=schema_version, mode=ScanMode.TARGETED,
        roots=tuple(ScanRoot(root_ids[n], f"/synthetic/{n}", RootOrigin.USER_SELECTED, label)
                    for n, label in roots.items()),
        started_at=BASE_TIME, finished_at=BASE_TIME, complete=True, stop_reason=None, limits_reached=(),
        limits=ResourceLimits(), unrecognized_extensions={},
        summary=InventorySummary(len(records), 0, len(directories), len(skipped), {}, 0, {}, {}, {}, {}, 0,
                                 False, 0, 0, {}),
    )
    return build_inventory(report, records, directories, skipped)
