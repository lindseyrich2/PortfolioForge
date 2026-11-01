"""Folder roles and the course → assignment/project hierarchy.

Bottom-up, every folder gets a role:

* **container** – collects unrelated work (scan roots, ``Downloads``,
  ``PycharmProjects``, a course folder holding several assignments). Never a
  group itself; course/term containers become ``course_collection`` groups.
* **unit** – a project or assignment folder. It claims its whole subtree,
  except clearly separate projects nested inside (own markers), which become
  child groups.

Top-down, units become groups, and loose files directly in containers are
clustered (see :mod:`.clustering`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..inventory.models import FileRecord
from .clustering import cluster_loose_files
from .config import GroupingConfig
from .features import AssignmentMarker, assignment_marker, course_codes, file_role, is_term_name
from .index import Folder, InventoryIndex
from .models import Evidence, GroupKind
from .scoring import stable_id

_DEPENDENCY_FILES = frozenset({
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "pipfile", "pipfile.lock", "poetry.lock",
    "environment.yml", "environment.yaml", "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "build.xml", "makefile", "cmakelists.txt",
    "cargo.toml", "go.mod", "gemfile", "description", "renv.lock", "manifest.mf", ".classpath", ".project",
    "tsconfig.json", "tox.ini",
})
_DEPENDENCY_SUFFIXES = (".rproj", ".sln", ".csproj", ".vcxproj", ".fsproj")
_IDE_DIRS = frozenset({".idea", ".vscode", "nbproject", ".settings", ".devcontainer"})
_ENV_DIRS = frozenset({"renv", "packrat"})
_VCS_DIRS = frozenset({".git", ".github", ".gitlab"})
_ASSIGNMENT_KIND_WORDS_EXCLUDED = frozenset({"project", "proj"})


@dataclass
class FolderRole:
    folder: Folder
    evidence: list[Evidence]
    score: float
    marker: AssignmentMarker | None
    courses: tuple[str, ...]
    is_collection_name: bool
    container: bool = False
    unit: bool = False
    strong: bool = False


@dataclass
class GroupBuilder:
    id: str
    kind: GroupKind
    title: str
    anchor: str | None
    parent_id: str | None
    evidence: list[Evidence] = field(default_factory=list)
    members: dict[str, list[Evidence]] = field(default_factory=dict)
    course_hints: tuple[str, ...] = ()
    status: str = "proposed"
    user_title: str | None = None
    labels: list[str] = field(default_factory=list)

    def add(self, record: FileRecord, evidence: list[Evidence]) -> None:
        self.members[record.id] = evidence


@dataclass
class Segmentation:
    groups: dict[str, GroupBuilder]
    unassigned: dict[str, str]
    """file id → reason"""
    roles: dict[str, FolderRole]


def folder_evidence(folder: Folder, index: InventoryIndex, config: GroupingConfig) -> list[Evidence]:
    ev: list[Evidence] = []
    ref = (folder.id,)
    if folder.marker_dirs & _VCS_DIRS:
        ev.append(Evidence("version_control", config.w("version_control"), "has a version-control folder (.git)", ref))
    if folder.marker_dirs & _IDE_DIRS:
        names = ", ".join(sorted(folder.marker_dirs & _IDE_DIRS))
        ev.append(Evidence("ide_project", config.w("ide_project"), f"has IDE project settings ({names})", ref))
    deps = {m for m in folder.marker_files if m in _DEPENDENCY_FILES or m.endswith(_DEPENDENCY_SUFFIXES)}
    if deps or folder.marker_dirs & _ENV_DIRS:
        shown = ", ".join(sorted(deps | (folder.marker_dirs & _ENV_DIRS)))
        ev.append(Evidence("dependency_file", config.w("dependency_file"), f"has project configuration ({shown})", ref))
    if any(m.startswith("readme") for m in folder.marker_files):
        ev.append(Evidence("readme", config.w("readme"), "has a README", ref))
    if any(m.startswith(("license", "copying")) or m == ".gitignore" for m in folder.marker_files):
        ev.append(Evidence("license_or_ignore", config.w("license_or_ignore"), "has a LICENSE or .gitignore", ref))
    if folder.skipped_child_reasons.get("virtual_env"):
        ev.append(Evidence("virtual_env", config.w("virtual_env"), "has a virtual environment (skipped by the scan)", ref))
    if folder.skipped_child_reasons.get("build_dir"):
        ev.append(Evidence("build_output", config.w("build_output"), "has build output (skipped by the scan)", ref))
    structural = [c for c in folder.children if index.folders[c].name.casefold() in config.structural_names]
    if len(structural) >= 2:
        ev.append(Evidence("structured_layout", config.w("structured_layout"),
                           f"has a project layout ({len(structural)} standard subfolders)", ref))
    return ev


def classify(index: InventoryIndex, config: GroupingConfig) -> dict[str, FolderRole]:
    roles: dict[str, FolderRole] = {}
    order = [f for root in index.roots for f in index.walk(root)]
    for folder in order:
        ev = folder_evidence(folder, index, config)
        roles[folder.id] = FolderRole(
            folder=folder,
            evidence=ev,
            score=sum(e.weight for e in ev),
            marker=None if folder.is_root else assignment_marker(folder.name, config),
            courses=() if folder.is_root else course_codes(folder.name, config),
            is_collection_name=not folder.is_root and (
                bool(course_codes(folder.name, config)) or is_term_name(folder.name, config)
                or bool(set(folder.name.casefold().split()) & config.collection_words)
            ),
        )
    for folder in reversed(order):  # children before parents
        role = roles[folder.id]
        role.strong = role.score >= config.t("nested_project")
        meaningful = [c for c in folder.children if index.folders[c].name.casefold() not in config.structural_names]
        child_units = [c for c in meaningful if roles[c].unit]
        child_containers = [c for c in meaningful if roles[c].container and index.subtree_file_count(c) > 0]
        has_files = index.subtree_file_count(folder.id) > 0
        named_unit = role.marker is not None or role.score >= config.t("project_folder")
        numbered_keys = {
            m.key for f in folder.files
            if (m := assignment_marker(f.stem, config)) is not None and m.number
        }
        loose_assignments = bool(numbered_keys)
        if folder.is_root:
            role.container = True
        elif folder.name.casefold() in config.container_names and not role.strong:
            role.container = True
        elif child_containers and not role.strong:
            role.container = True  # a folder holding a container is itself a container
        elif role.is_collection_name and not role.strong and (child_units or loose_assignments):
            role.container = True  # a course or term folder
        elif len(numbered_keys) >= 2 and not role.strong:
            role.container = True  # loose files for several assignments: split them
        elif len(child_units) >= 2 and not role.strong:
            role.container = True
        elif not folder.files and child_units and not named_unit:
            role.container = True  # a plain folder that only holds other units
        elif not has_files:
            role.container = True  # empty: nothing to group
        else:
            role.unit = True
    return roles


def segment(index: InventoryIndex, config: GroupingConfig) -> Segmentation:
    roles = classify(index, config)
    groups: dict[str, GroupBuilder] = {}
    unassigned: dict[str, str] = {}

    def new_unit(folder: Folder, parent_id: str | None) -> GroupBuilder:
        role = roles[folder.id]
        marker = role.marker
        kind = GroupKind.ASSIGNMENT if marker and (marker.number or marker.word not in _ASSIGNMENT_KIND_WORDS_EXCLUDED) \
            else GroupKind.PROJECT
        evidence = [Evidence("shared_folder", config.w("shared_folder"), f"files share the folder '{folder.name}'",
                             (folder.id,)), *role.evidence]
        if marker:
            evidence.append(Evidence("assignment_name", config.w("assignment_name"),
                                     f"folder name looks like an assignment ('{marker.key}')", (folder.id,)))
        builder = GroupBuilder(
            id=stable_id("g", "unit", folder.id), kind=kind, title=folder.name,
            anchor=folder.id, parent_id=parent_id, evidence=evidence,
            course_hints=_course_hints(folder, index, config),
        )
        groups[builder.id] = builder
        return builder

    def walk(fid: str, collection: str | None, enclosing: GroupBuilder | None) -> None:
        folder = index.folders[fid]
        role = roles[fid]
        if enclosing is not None:
            if role.unit and role.strong and folder.id != enclosing.anchor:
                child = new_unit(folder, enclosing.id)
                _claim(child, folder, folder, config)
                for c in folder.children:
                    walk(c, collection, child)
                return
            anchor = index.folders[enclosing.anchor] if enclosing.anchor else folder
            _claim(enclosing, folder, anchor, config)
            for c in folder.children:
                walk(c, collection, enclosing)
            return
        if role.container:
            current = collection
            if role.is_collection_name and index.subtree_file_count(fid) > 0:
                col = GroupBuilder(
                    id=stable_id("g", "collection", fid), kind=GroupKind.COURSE_COLLECTION,
                    title=folder.name, anchor=fid, parent_id=collection,
                    evidence=[Evidence("course_folder", 2.5 if role.courses else 2.0,
                                       f"folder name looks like a {'course' if role.courses else 'course or term'} "
                                       f"('{folder.name}') and holds several pieces of work", (fid,))],
                    course_hints=_course_hints(folder, index, config),
                )
                groups[col.id] = col
                current = col.id
            clustered: set[str] = set()
            for builder in cluster_loose_files(folder, current, config, _course_hints(folder, index, config)):
                groups[builder.id] = builder
                clustered.update(builder.members)
            for record in folder.files:
                if record.id not in clustered:
                    unassigned[record.id] = "no grouping evidence (loose file)"
            for c in folder.children:
                walk(c, current, None)
            return
        unit = new_unit(folder, collection)
        _claim(unit, folder, folder, config)
        for c in folder.children:
            walk(c, collection, unit)

    for root in index.roots:
        walk(root, None, None)
    return Segmentation(groups, unassigned, roles)


def _claim(group: GroupBuilder, folder: Folder, anchor: Folder, config: GroupingConfig) -> None:
    for record in folder.files:
        if folder.id == anchor.id:
            ev = [Evidence("in_group_folder", 2.0, f"directly in the group's folder '{anchor.name}'", (anchor.id,))]
        else:
            sub = folder.rel[len(anchor.rel):].lstrip("/") if anchor.rel else folder.rel
            ev = [Evidence("in_group_subfolder", 1.5, f"in the group's subfolder '{sub}'", (folder.id,))]
        group.add(record, ev)


def _course_hints(folder: Folder, index: InventoryIndex, config: GroupingConfig) -> tuple[str, ...]:
    hints = list(course_codes(folder.name, config)) if not folder.is_root else []
    for ancestor in index.ancestors(folder.id):
        if not ancestor.is_root:
            hints.extend(course_codes(ancestor.name, config))
    return tuple(dict.fromkeys(hints))


__all__ = ["FolderRole", "GroupBuilder", "Segmentation", "classify", "segment", "file_role"]
