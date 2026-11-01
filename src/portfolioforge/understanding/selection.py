"""Proposing which files of a group could be analyzed, and recording approval.

The proposal uses metadata only. Approval (Gate 1) is always explicit and
records each file's size and modification time, so a file that changes later
can no longer be read under that approval. Credentials, private folders and
sensitive names are excluded and cannot be approved.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..grouping.models import CandidateGroup, GroupStatus
from ..grouping.scoring import stable_id
from ..inventory import rules
from ..inventory.models import FileRecord, Inventory
from ..inventory.roots import is_private_location
from ..privacy import PrivacySettings
from .models import (
    ApprovedFile,
    Eligibility,
    EligibilityStatus,
    ExcludedFile,
    FileApprovalRecord,
    SelectedFile,
    SelectionProposal,
)

#: Languages with local evidence extraction.
SUPPORTED_EXTENSIONS: dict[str, tuple[str, str]] = {
    ".py": ("Python", "source"),
    ".ipynb": ("Jupyter Notebook", "notebook"),
    ".java": ("Java", "source"),
    ".r": ("R", "source"),
    ".rmd": ("R Markdown", "source"),
    ".sql": ("SQL", "source"),
}
#: Supporting files that describe a project (by exact name, case-insensitive).
SUPPORTING_NAMES: dict[str, str] = {
    "readme.md": "README", "readme.txt": "README", "readme.rst": "README", "readme": "README",
    "requirements.txt": "requirements",
}
MAX_SOURCE_BYTES = 1_000_000
MAX_NOTEBOOK_BYTES = 5_000_000


class ApprovalError(ValueError):
    """Raised when files cannot be approved for local reading."""


def project_path(record: FileRecord, group: CandidateGroup, inventory: Inventory) -> str:
    """Path inside the project, without the folders above the group's own folder."""
    anchor_rel = (group.anchor or "").partition(":")[2] if group.summary.get("folder_unit") else None
    if anchor_rel and record.relative_path.startswith(anchor_rel + "/"):
        return record.relative_path[len(anchor_rel) + 1:]
    if anchor_rel == "":
        return record.relative_path
    return record.name


def exclusion_reason(record: FileRecord, privacy: PrivacySettings) -> str | None:
    """Why a file may never be opened, regardless of approval."""
    if rules.is_sensitive_name(record.name):
        return "sensitive file name"
    # Folder names are checked below the scan root (the root itself was validated when the
    # scan was planned); configured private paths are checked against the absolute path.
    parts = [p.casefold() for p in record.path_parts]
    if any(p in rules.CREDENTIAL_DIR_NAMES for p in parts):
        return "inside a credential location"
    if any(p in privacy.private_folder_names for p in parts) or \
            is_private_location(record.absolute_path, privacy.private_paths, frozenset()):
        return "inside a private folder"
    return None


def propose_selection(group: CandidateGroup, inventory: Inventory, privacy: PrivacySettings) -> SelectionProposal:
    files = {f.id: f for f in inventory.files}
    selected, excluded = [], []
    for m in group.members:
        record = files.get(m.file_id)
        if record is None:
            continue
        path = project_path(record, group, inventory)
        blocked = exclusion_reason(record, privacy)
        if blocked:
            shown = rules.REDACTED if blocked == "sensitive file name" else path
            excluded.append(ExcludedFile(record.id, shown, blocked))
            continue
        ext = record.extension
        if ext in SUPPORTED_EXTENSIONS:
            language, role = SUPPORTED_EXTENSIONS[ext]
            limit = MAX_NOTEBOOK_BYTES if role == "notebook" else MAX_SOURCE_BYTES
            if record.size_bytes > limit:
                excluded.append(ExcludedFile(record.id, path, f"larger than {limit // 1_000_000} MB"))
                continue
            selected.append(SelectedFile(record.id, path, language, record.size_bytes, role))
        elif record.name.casefold() in SUPPORTING_NAMES and record.size_bytes <= MAX_SOURCE_BYTES:
            selected.append(SelectedFile(record.id, path, SUPPORTING_NAMES[record.name.casefold()],
                                         record.size_bytes, "supporting"))
        else:
            excluded.append(ExcludedFile(record.id, path, _unsupported_reason(record)))
    return SelectionProposal(group.id, tuple(selected), tuple(excluded))


def _unsupported_reason(record: FileRecord) -> str:
    category = record.category.value
    if category in ("data", "spreadsheet"):
        return "data file (not sent; schema extraction is not implemented)"
    if category in ("document", "presentation"):
        return "document (not read unless supported in a later version)"
    if category == "archive":
        return "archive (never opened)"
    return f"no evidence extractor for {record.extension or 'this file type'}"


def require_approvable(group: CandidateGroup, eligibility: Eligibility, acknowledge_review: bool) -> None:
    """The conditions every approval to read files locally shares."""
    if group.status is not GroupStatus.ACCEPTED:
        raise ApprovalError("Accept the group first (python -m portfolioforge.grouping accept <GROUP>).")
    if eligibility.status in (EligibilityStatus.PERSONAL_OR_ADMIN, EligibilityStatus.EXCLUDED):
        raise ApprovalError(f"This group is {eligibility.status.value} and cannot be approved for analysis.")
    if eligibility.status is EligibilityStatus.NEEDS_REVIEW and not acknowledge_review:
        raise ApprovalError("This group needs review. Check its reasons, then approve with the review acknowledgement.")


def approve_files(
    *,
    inventory_key: str,
    group: CandidateGroup,
    eligibility: Eligibility,
    proposal: SelectionProposal,
    inventory: Inventory,
    file_ids: list[str] | None = None,
    acknowledge_review: bool = False,
    now: datetime | None = None,
) -> FileApprovalRecord:
    """Record the user's explicit approval to read selected files locally.

    Raises ApprovalError unless the group is accepted, eligible, and every
    requested file is in the proposal's selected list.
    """
    require_approvable(group, eligibility, acknowledge_review)
    selectable = {s.file_id: s for s in proposal.selected}
    chosen = list(dict.fromkeys(file_ids)) if file_ids else list(selectable)
    if not chosen:
        raise ApprovalError("No analyzable files to approve.")
    unknown = [f for f in chosen if f not in selectable]
    if unknown:
        raise ApprovalError(f"{len(unknown)} file(s) are not in this group's selectable files.")
    files = {f.id: f for f in inventory.files}
    approved = tuple(
        ApprovedFile(fid, files[fid].absolute_path, selectable[fid].project_path, selectable[fid].language,
                     files[fid].size_bytes, files[fid].modified_at.isoformat() if files[fid].modified_at else None)
        for fid in chosen
    )
    created = now or datetime.now(UTC)
    return FileApprovalRecord(
        id=stable_id("a", inventory_key, group.id, created.isoformat(), *chosen),
        inventory_key=inventory_key, group_id=group.id, files=approved, approved_at=created,
        eligibility_status=eligibility.status, acknowledged_review=acknowledge_review,
    )
