"""Folder tree built from saved inventory records (schema v1 or v2). No file access."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterator

from ..inventory import rules
from ..inventory.models import EntryKind, FileRecord, Inventory, SkipReason
from ..inventory.rules import REDACTED
from ..privacy import PRIVATE_PLACEHOLDER


def folder_id(root_id: str, relative_path: str) -> str:
    return f"{root_id}:{relative_path}"


@dataclass
class Folder:
    id: str
    root_id: str
    rel: str
    name: str
    parent: str | None
    depth: int
    children: list[str] = field(default_factory=list)
    files: list[FileRecord] = field(default_factory=list)
    marker_files: set[str] = field(default_factory=set)
    """Casefolded project-marker file names (v2 records, or inventoried marker files in v1)."""
    marker_dirs: set[str] = field(default_factory=set)
    """Casefolded project-marker folder names (v2 records, or skipped folders in v1)."""
    skipped_child_reasons: Counter[str] = field(default_factory=Counter)

    @property
    def is_root(self) -> bool:
        return self.rel == ""


class InventoryIndex:
    def __init__(self, inventory: Inventory) -> None:
        self.inventory = inventory
        self.files: dict[str, FileRecord] = {f.id: f for f in inventory.files}
        self.folders: dict[str, Folder] = {}
        self.root_labels = {r.id: r.label for r in inventory.report.roots}
        self.root_paths = {r.id: r.path for r in inventory.report.roots}
        for d in inventory.directories:
            folder = self._folder(d.root_id, d.relative_path, d.name)
            folder.marker_files.update(n.casefold() for n in d.marker_files)
            folder.marker_dirs.update(n.casefold() for n in d.marker_dirs)
        for f in inventory.files:
            folder = self._folder(f.root_id, f.parent_dir)
            folder.files.append(f)
            if rules.is_project_marker_file(f.name):
                folder.marker_files.add(f.name.casefold())
        for s in inventory.skipped:
            if s.kind is not EntryKind.DIRECTORY or s.root_id is None or not s.relative_path:
                continue
            parent_rel, _, name = s.relative_path.rpartition("/")
            if name in (REDACTED, PRIVATE_PLACEHOLDER):
                continue
            parent = self._folder(s.root_id, parent_rel)
            if rules.is_project_marker_dir(name):
                parent.marker_dirs.add(name.casefold())
            if s.reason in (SkipReason.VIRTUAL_ENV, SkipReason.BUILD_DIR):
                parent.skipped_child_reasons[s.reason.value] += 1
        for folder in self.folders.values():
            folder.files.sort(key=lambda r: r.relative_path)
            folder.children.sort()
        self.roots = sorted(fid for fid, f in self.folders.items() if f.is_root)
        self._subtree_counts: dict[str, int] = {}

    def _folder(self, root_id: str, rel: str, name: str | None = None) -> Folder:
        fid = folder_id(root_id, rel)
        existing = self.folders.get(fid)
        if existing is not None:
            return existing
        parent_rel, _, last = rel.rpartition("/")
        parent = None if rel == "" else folder_id(root_id, parent_rel)
        display = name if name is not None else (last if rel else self.root_labels.get(root_id, root_id))
        folder = Folder(fid, root_id, rel, display, parent, 0 if rel == "" else rel.count("/") + 1)
        self.folders[fid] = folder
        if rel != "":
            parent_folder = self._folder(root_id, parent_rel)
            parent_folder.children.append(fid)
        return folder

    def walk(self, start: str) -> Iterator[Folder]:
        stack = [start]
        while stack:
            folder = self.folders[stack.pop()]
            yield folder
            stack.extend(reversed(folder.children))

    def subtree_file_count(self, fid: str) -> int:
        if fid not in self._subtree_counts:
            self._subtree_counts[fid] = sum(len(f.files) for f in self.walk(fid))
        return self._subtree_counts[fid]

    def ancestors(self, fid: str) -> Iterator[Folder]:
        current = self.folders[fid].parent
        while current is not None:
            folder = self.folders[current]
            yield folder
            current = folder.parent

    def display_path(self, fid: str) -> str:
        folder = self.folders[fid]
        label = self.root_labels.get(folder.root_id, folder.root_id)
        return f"{label}/{folder.rel}" if folder.rel else label
