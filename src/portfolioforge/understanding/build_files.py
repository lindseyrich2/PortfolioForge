"""Finding build configuration files, and their own approval.

The saved inventory does not list every build file, because it only records extensions it
knows. So this module checks the *fixed list of exact names* the project's inspector supplies,
inside an accepted project's folder, with ``lstat`` only. It never lists a folder, never opens
a file, and never looks above the project.

Approval is separate from Gate 1 and stored separately: approving source files does not allow
reading build files, and these files can never reach an evidence pack by that route. Files
that often hold credentials are noted by name for your review and can never be approved here.

When no inspector supports the project, nothing is offered and manual review is needed.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from ..grouping.models import CandidateGroup
from ..grouping.scoring import stable_id
from ..inventory import rules
from ..inventory.models import Inventory
from ..inventory.platform import CLOUD_FILE_ATTRIBUTES, file_attributes, is_link_stat
from ..inventory.roots import is_private_location
from ..privacy import PrivacySettings
from .inspectors import DEFAULT_REGISTRY, MANUAL_REVIEW_REASON, InspectorRegistry, ProjectFacts, ProjectInspector
from .models import (
    BUILD_APPROVAL_SCOPE,
    ApprovedFile,
    BuildFileCandidate,
    BuildFileProposal,
    Eligibility,
    ExcludedFile,
    FileApprovalRecord,
)
from .selection import ApprovalError, project_path as member_project_path
from .selection import require_approvable

MAX_BUILD_FILE_BYTES = 2_000_000
BUILD_FILE_LANGUAGE = "Build configuration"


def project_folder(group: CandidateGroup) -> tuple[str, str] | None:
    """(root id, relative folder) of a folder project, or None for a cluster of loose files."""
    if not group.anchor or not group.summary.get("folder_unit"):
        return None
    root_id, _, relative = group.anchor.partition(":")
    return root_id, relative


def project_facts(group: CandidateGroup, inventory: Inventory) -> ProjectFacts:
    """Metadata an inspector can use to decide whether it applies. Reads nothing."""
    records = {f.id: f for f in inventory.files}
    members = tuple(member_project_path(records[m.file_id], group, inventory) for m in group.members
                    if m.file_id in records)
    marker_files: set[str] = set()
    marker_dirs: set[str] = set()
    located = project_folder(group)
    if located:
        root_id, anchor = located
        prefix = anchor + "/" if anchor else ""
        for d in inventory.directories:
            if d.root_id == root_id and (d.relative_path == anchor or d.relative_path.startswith(prefix)):
                marker_files.update(n.casefold() for n in d.marker_files)
                marker_dirs.update(n.casefold() for n in d.marker_dirs)
    return ProjectFacts(members, frozenset(marker_files), frozenset(marker_dirs))


def _build_roots(group: CandidateGroup, inventory: Inventory, root_id: str, anchor: str,
                 inspector: ProjectInspector) -> list[str]:
    """Folders (relative to the project) that may hold build files.

    A subfolder counts only if the inventory marks it as a build root and this group owns
    files inside it, so a separate project nested in the folder is not swept in.
    """
    members = {m.file_id for m in group.members}
    prefix = anchor + "/" if anchor else ""
    owned = [f.relative_path for f in inventory.files if f.id in members]
    found = {""}
    for d in inventory.directories:
        if d.root_id != root_id or not d.relative_path or d.relative_path == anchor \
                or not d.relative_path.startswith(prefix):
            continue
        has_marker = {n.casefold() for n in d.marker_files} & inspector.root_marker_files or \
            {n.casefold() for n in d.marker_dirs} & inspector.root_marker_dirs
        if has_marker and any(p.startswith(d.relative_path + "/") for p in owned):
            found.add(d.relative_path[len(prefix):])
    return sorted(found)


def _lstat_chain(project_dir: str, relative: str) -> os.stat_result | None:
    """lstat a path, stopping at the first link so nothing beyond a link is ever touched."""
    current = project_dir
    result = None
    for part in relative.split("/"):
        current = os.path.join(current, part)
        try:
            result = os.lstat(current)
        except OSError:
            return None
        if is_link_stat(result):
            return result
    return result


def propose_build_files(group: CandidateGroup, inventory: Inventory, privacy: PrivacySettings,
                        registry: InspectorRegistry = DEFAULT_REGISTRY) -> BuildFileProposal:
    """What could be approved. Uses file metadata only; opens nothing."""
    inspector = registry.select(project_facts(group, inventory))
    if inspector is None:
        return BuildFileProposal(group.id, (), (), (), (), (MANUAL_REVIEW_REASON,), None)

    def empty(note: str) -> BuildFileProposal:
        return BuildFileProposal(group.id, (), (), (), (), (note,), inspector.name)

    located = project_folder(group)
    if located is None:
        return empty("This group is not a single project folder, so it has no build files to look for.")
    root_id, anchor = located
    root_path = next((r.path for r in inventory.report.roots if r.id == root_id), None)
    if root_path is None:
        return empty("The project's scan root is not in this inventory.")
    project_dir = os.path.join(root_path, *anchor.split("/")) if anchor else root_path
    if is_private_location(project_dir, privacy.private_paths, privacy.private_folder_names):
        return empty("This project is inside a private folder.")

    candidates: list[BuildFileCandidate] = []
    blocked: list[ExcludedFile] = []
    review_only: list[str] = []
    present: list[str] = []
    for build_root in _build_roots(group, inventory, root_id, anchor, inspector):
        prefix = build_root + "/" if build_root else ""
        for relative, role in inspector.build_files.items():
            path = prefix + relative
            info = _lstat_chain(project_dir, path)
            if info is None:
                continue
            file_id = stable_id("bf", group.id, path)
            reason = _blocked_reason(info, path, project_dir, privacy)
            if reason:
                blocked.append(ExcludedFile(file_id, path, reason))
                continue
            candidates.append(BuildFileCandidate(
                file_id, path, role, info.st_size, os.path.join(project_dir, *path.split("/")),
                datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
            ))
        for relative in inspector.review_only_files:
            if _lstat_chain(project_dir, prefix + relative) is not None:
                review_only.append(prefix + relative)
        for relative in inspector.presence_checks:
            if relative not in present and _lstat_chain(project_dir, prefix + relative) is not None:
                present.append(relative)
    return BuildFileProposal(group.id, tuple(candidates), tuple(blocked), tuple(review_only), tuple(present), (),
                             inspector.name)


def _blocked_reason(info: os.stat_result, path: str, project_dir: str, privacy: PrivacySettings) -> str | None:
    if is_link_stat(info) or not stat.S_ISREG(info.st_mode):
        return "not a regular file (links are never followed)"
    if file_attributes(info) & CLOUD_FILE_ATTRIBUTES:
        return "cloud-only placeholder (reading it would download it)"
    if info.st_size > MAX_BUILD_FILE_BYTES:
        return f"larger than {MAX_BUILD_FILE_BYTES // 1_000_000} MB"
    if rules.is_sensitive_name(Path(path).name):
        return "sensitive file name"
    full = os.path.join(project_dir, *path.split("/"))
    if is_private_location(full, privacy.private_paths, privacy.private_folder_names):
        return "inside a private folder"
    return None


def approve_build_files(
    *,
    inventory_key: str,
    group: CandidateGroup,
    eligibility: Eligibility,
    proposal: BuildFileProposal,
    file_ids: list[str] | None = None,
    acknowledge_review: bool = False,
    now: datetime | None = None,
) -> FileApprovalRecord:
    """Record explicit approval to read these build files on this computer."""
    require_approvable(group, eligibility, acknowledge_review)
    selectable = {c.file_id: c for c in proposal.candidates}
    chosen = list(dict.fromkeys(file_ids)) if file_ids else list(selectable)
    if not chosen:
        raise ApprovalError("No build configuration files were found to approve.")
    unknown = [f for f in chosen if f not in selectable]
    if unknown:
        raise ApprovalError(f"{len(unknown)} file(s) are not this group's recognized build files.")
    created = now or datetime.now(UTC)
    return FileApprovalRecord(
        id=stable_id("ba", inventory_key, group.id, created.isoformat(), *chosen),
        inventory_key=inventory_key, group_id=group.id, approved_at=created, eligibility_status=eligibility.status,
        acknowledged_review=acknowledge_review, scope=BUILD_APPROVAL_SCOPE,
        files=tuple(
            ApprovedFile(fid, selectable[fid].absolute_path, selectable[fid].project_path, BUILD_FILE_LANGUAGE,
                         selectable[fid].size_bytes, selectable[fid].modified_at)
            for fid in chosen
        ),
    )
