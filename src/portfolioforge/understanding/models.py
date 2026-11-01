"""Records for the project-understanding preparation stage.

Two approval gates protect original content:

* **Gate 1, file approval** (:class:`FileApprovalRecord`): which files of an
  accepted group may be opened *locally* to extract evidence.
* **Gate 2, disclosure approval** (:class:`ManifestApproval`): the exact payload
  (by SHA-256) that may be handed to one named provider and model.

All records are stored locally, outside any Git repository.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import Enum, StrEnum
from typing import Any

UNDERSTANDING_SCHEMA_VERSION = 1


class EligibilityStatus(StrEnum):
    TECHNICAL_CANDIDATE = "technical_candidate"
    """Looks like a technical project. Still needs the user's file approval."""
    NEEDS_REVIEW = "needs_review"
    """Uncertain. Never selected automatically; approval needs an explicit acknowledgement."""
    PERSONAL_OR_ADMIN = "personal_or_admin"
    """Looks like personal or administrative documents. Cannot be approved for analysis."""
    EXCLUDED = "excluded"
    """The user labelled it ignored or third-party, or it is not analyzable."""


@dataclass(frozen=True, slots=True)
class Eligibility:
    group_id: str
    status: EligibilityStatus
    reasons: tuple[str, ...]
    code_files: int
    notebook_files: int
    document_files: int
    total_files: int


@dataclass(frozen=True, slots=True)
class SelectedFile:
    file_id: str
    project_path: str
    """Path relative to the group's folder (no folders above the project)."""
    language: str
    size_bytes: int
    role: str
    """source | notebook | supporting"""


@dataclass(frozen=True, slots=True)
class ExcludedFile:
    file_id: str
    project_path: str
    """``[redacted]`` for sensitive names."""
    reason: str


@dataclass(frozen=True, slots=True)
class SelectionProposal:
    group_id: str
    selected: tuple[SelectedFile, ...]
    excluded: tuple[ExcludedFile, ...]


@dataclass(frozen=True, slots=True)
class ApprovedFile:
    file_id: str
    absolute_path: str
    project_path: str
    language: str
    size_bytes: int
    modified_at: str | None
    """Metadata at approval time; a file that changed since cannot be read."""


@dataclass(frozen=True, slots=True)
class FileApprovalRecord:
    id: str
    inventory_key: str
    group_id: str
    files: tuple[ApprovedFile, ...]
    approved_at: datetime
    eligibility_status: EligibilityStatus
    acknowledged_review: bool
    scope: str = "read_locally_for_evidence"
    """Approval to read these files on this computer only. Not approval to send anything."""


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    id: str
    file_id: str
    project_path: str
    language: str
    kind: str
    """import | definition | docstring | comment | markdown | statement | dependency | readme | chunk"""
    line_start: int
    line_end: int
    cell: int | None
    """Notebook cell number (1-based) for notebook evidence."""
    text: str
    """Redacted excerpt."""
    sha256: str
    """Hash of the redacted excerpt, so later claims can be checked against it."""
    redactions: dict[str, int] = field(default_factory=dict)

    @property
    def location(self) -> str:
        lines = f"line {self.line_start}" if self.line_start == self.line_end else \
            f"lines {self.line_start}-{self.line_end}"
        return f"{self.project_path} cell {self.cell}, {lines}" if self.cell else f"{self.project_path} {lines}"


@dataclass(frozen=True, slots=True)
class Technology:
    name: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvidencePack:
    id: str
    inventory_key: str
    group_id: str
    approval_id: str
    created_at: datetime
    items: tuple[EvidenceItem, ...]
    technologies: tuple[Technology, ...]
    files: tuple[dict[str, Any], ...]
    """Per file: project path, language, evidence count, status."""
    warnings: tuple[str, ...]
    schema_version: int = UNDERSTANDING_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ManifestItem:
    evidence_id: str
    location: str
    kind: str
    bytes: int
    redactions: dict[str, int]


@dataclass(frozen=True, slots=True)
class DisclosureManifest:
    id: str
    inventory_key: str
    group_id: str
    evidence_pack_id: str
    provider: str
    model: str
    items: tuple[ManifestItem, ...]
    not_sent: tuple[str, ...]
    """Human-readable list of what is deliberately left out."""
    payload_bytes: int
    estimated_tokens: int
    payload_sha256: str
    created_at: datetime
    notice: str


@dataclass(frozen=True, slots=True)
class ManifestApproval:
    id: str
    manifest_id: str
    payload_sha256: str
    provider: str
    model: str
    approved_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider: str
    model: str
    sent: bool
    """False for the dry-run provider: nothing left the computer."""
    payload_bytes: int
    payload_sha256: str
    status: str
    response: str | None = None


def to_jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    return value


def dumps(value: Any) -> str:
    return json.dumps(to_jsonable(value), indent=2, ensure_ascii=False)


# --- loading ----------------------------------------------------------------------


def approval_from_dict(d: dict[str, Any]) -> FileApprovalRecord:
    return FileApprovalRecord(
        id=d["id"], inventory_key=d["inventory_key"], group_id=d["group_id"],
        files=tuple(ApprovedFile(**f) for f in d["files"]), approved_at=datetime.fromisoformat(d["approved_at"]),
        eligibility_status=EligibilityStatus(d["eligibility_status"]), acknowledged_review=d["acknowledged_review"],
        scope=d.get("scope", "read_locally_for_evidence"),
    )


def pack_from_dict(d: dict[str, Any]) -> EvidencePack:
    if d.get("schema_version") != UNDERSTANDING_SCHEMA_VERSION:
        raise ValueError("Unsupported evidence pack version.")
    return EvidencePack(
        id=d["id"], inventory_key=d["inventory_key"], group_id=d["group_id"], approval_id=d["approval_id"],
        created_at=datetime.fromisoformat(d["created_at"]),
        items=tuple(EvidenceItem(**{**i, "redactions": dict(i.get("redactions", {}))}) for i in d["items"]),
        technologies=tuple(Technology(t["name"], tuple(t["evidence_ids"])) for t in d["technologies"]),
        files=tuple(dict(f) for f in d["files"]), warnings=tuple(d["warnings"]), schema_version=d["schema_version"],
    )


def manifest_from_dict(d: dict[str, Any]) -> DisclosureManifest:
    return DisclosureManifest(
        **{**d, "items": tuple(ManifestItem(**{**i, "redactions": dict(i["redactions"])}) for i in d["items"]),
           "not_sent": tuple(d["not_sent"]), "created_at": datetime.fromisoformat(d["created_at"])}
    )


def manifest_approval_from_dict(d: dict[str, Any]) -> ManifestApproval:
    return ManifestApproval(**{**d, "approved_at": datetime.fromisoformat(d["approved_at"])})
