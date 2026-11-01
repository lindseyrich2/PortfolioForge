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
    request: dict[str, Any] = field(default_factory=dict)
    """Request parameters shown to the user (format, max_tokens, effort, external)."""


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
           "not_sent": tuple(d["not_sent"]), "created_at": datetime.fromisoformat(d["created_at"]),
           "request": dict(d.get("request", {}))}
    )


def manifest_approval_from_dict(d: dict[str, Any]) -> ManifestApproval:
    return ManifestApproval(**{**d, "approved_at": datetime.fromisoformat(d["approved_at"])})


# --- local build-and-run inspection -------------------------------------------------------
#
# Build configuration has its own approval record (a FileApprovalRecord with
# BUILD_APPROVAL_SCOPE, stored under a different file name), so Gate 1 and the evidence
# pack can never pick it up. Executing anything needs a separate, single-use approval.

BUILD_APPROVAL_SCOPE = "read_build_configuration_locally"
INSPECTION_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class BuildFileCandidate:
    file_id: str
    project_path: str
    role: str
    size_bytes: int
    absolute_path: str
    modified_at: str | None


@dataclass(frozen=True, slots=True)
class BuildFileProposal:
    group_id: str
    candidates: tuple[BuildFileCandidate, ...]
    blocked: tuple[ExcludedFile, ...]
    """Recognized names that exist but may not be read (links, cloud-only, too large)."""
    review_only: tuple[str, ...]
    """Configuration files that often hold credentials. Seen by name only; never offered or read."""
    present: tuple[str, ...]
    """Which of the inspector's presence checks exist. Seen by name only."""
    notes: tuple[str, ...]
    inspector: str | None = None
    """The inspector chosen for this project, or None when manual review is needed."""


@dataclass(frozen=True, slots=True)
class SourceRef:
    project_path: str
    line: int | None
    detail: str

    @property
    def location(self) -> str:
        return f"{self.project_path} line {self.line}" if self.line else self.project_path


@dataclass(frozen=True, slots=True)
class EntryPoint:
    name: str
    status: str
    """confirmed (declared and seen in source) | source_only (seen in source) |
    declared_only (a build file names it, but it was not seen in an inspected source)."""
    found_in_source: bool
    source: SourceRef | None
    declared_by: tuple[SourceRef, ...] = ()
    note: str | None = None


@dataclass(frozen=True, slots=True)
class PackageInfo:
    name: str
    """Empty for the default package."""
    files: int
    location_matches: bool
    """Whether every file sits in the folder its package declaration requires."""


@dataclass(frozen=True, slots=True)
class ToolRequirement:
    setting: str
    value: str
    source: SourceRef


@dataclass(frozen=True, slots=True)
class CommandFinding:
    purpose: str
    """compile | package | run | test"""
    command: str
    status: str
    """supported (the inspected files back it) | guess (they do not). Neither means it was run."""
    basis: str
    """build_file | source_structure | none"""
    evidence: tuple[SourceRef, ...]
    reason: str
    working_dir: str = ""


@dataclass(frozen=True, slots=True)
class SensitiveFinding:
    kind: str
    confidence: str
    """high | medium | low"""
    project_path: str
    line: int | None
    detail: str
    """Describes the rule. Never contains the matched value."""


@dataclass(frozen=True, slots=True)
class RiskSignal:
    kind: str
    project_path: str
    line: int | None
    detail: str


@dataclass(frozen=True, slots=True)
class BuildInspection:
    id: str
    inventory_key: str
    group_id: str
    created_at: datetime
    source_approval_id: str | None
    build_approval_id: str | None
    inspector: str | None
    """Which inspector ran. None means no inspector supports this project type."""
    manual_review_reason: str | None
    """Set when no inspector applied: nothing below says how to build or run the project."""
    build_systems: tuple[str, ...]
    files: tuple[dict[str, Any], ...]
    """Per approved file: project path, what it was approved under, status."""
    entry_points: tuple[EntryPoint, ...]
    packages: tuple[PackageInfo, ...]
    source_roots: tuple[str, ...]
    test_roots: tuple[str, ...]
    options: dict[str, str]
    """Checked values the inspector needs again to build validation commands."""
    requirements: tuple[ToolRequirement, ...]
    dependencies: tuple[SourceRef, ...]
    commands: tuple[CommandFinding, ...]
    sensitive: tuple[SensitiveFinding, ...]
    risks: tuple[RiskSignal, ...]
    not_inspected: tuple[str, ...]
    """Which project files this inspection did not read. A clean scan says nothing about them."""
    notes: tuple[str, ...]
    schema_version: int = INSPECTION_SCHEMA_VERSION

    def commands_for(self, purpose: str, status: str | None = None) -> tuple[CommandFinding, ...]:
        return tuple(c for c in self.commands if c.purpose == purpose and (status is None or c.status == status))


@dataclass(frozen=True, slots=True)
class PlannedFile:
    file_id: str
    project_path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ValidationPlan:
    id: str
    inventory_key: str
    group_id: str
    inspection_id: str
    source_approval_id: str
    inspector: str
    action: str
    """compile | run. Approving one never authorizes the other."""
    entry_point: str | None
    options: dict[str, str]
    files: tuple[PlannedFile, ...]
    commands: tuple[tuple[str, ...], ...]
    timeout_seconds: int
    warnings: tuple[str, ...]
    runs_project_code: bool
    created_at: datetime
    plan_sha256: str


@dataclass(frozen=True, slots=True)
class ValidationApproval:
    id: str
    plan_id: str
    plan_sha256: str
    action: str
    approved_at: datetime
    used_at: datetime | None = None
    """Each approval allows one execution."""


@dataclass(frozen=True, slots=True)
class ValidationStep:
    label: str
    command: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    stdout: str
    """Redacted, truncated excerpt."""
    stderr: str
    redactions: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    id: str
    plan_id: str
    inspection_id: str
    inventory_key: str
    group_id: str
    action: str
    entry_point: str | None
    outcome: str
    """compile_passed | compile_failed | compile_timed_out | run_exit_zero | run_nonzero |
    run_timed_out | tool_missing"""
    tool_versions: dict[str, str]
    steps: tuple[ValidationStep, ...]
    notes: tuple[str, ...]
    copy_removed: bool
    finished_at: datetime


@dataclass(frozen=True, slots=True)
class LocalFindings:
    """What a proposal took from a local inspection. Rendered into the draft README."""

    inspection_id: str
    applied_at: datetime
    entry_points: tuple[EntryPoint, ...]
    requirements: tuple[ToolRequirement, ...]
    commands: tuple[CommandFinding, ...]
    validations: tuple[ValidationResult, ...] = ()
    resolved_flags: tuple[str, ...] = ()
    """Review flags this inspection settled; restored if another inspection replaces it."""
    added_flags: tuple[str, ...] = ()
    """Review flags this inspection raised; replaced if another inspection replaces it."""
    manual_review_reason: str | None = None


def _ref(d: dict[str, Any] | None) -> SourceRef | None:
    return SourceRef(**d) if d else None


def _entry(d: dict[str, Any]) -> EntryPoint:
    return EntryPoint(**{**d, "source": _ref(d["source"]),
                         "declared_by": tuple(SourceRef(**r) for r in d["declared_by"])})


def _requirement(d: dict[str, Any]) -> ToolRequirement:
    return ToolRequirement(d["setting"], d["value"], SourceRef(**d["source"]))


def _command(d: dict[str, Any]) -> CommandFinding:
    return CommandFinding(**{**d, "evidence": tuple(SourceRef(**r) for r in d["evidence"])})


def inspection_from_dict(d: dict[str, Any]) -> BuildInspection:
    if d.get("schema_version") != INSPECTION_SCHEMA_VERSION:
        raise ValueError("Unsupported build inspection version.")
    return BuildInspection(**{
        **d, "created_at": datetime.fromisoformat(d["created_at"]),
        "build_systems": tuple(d["build_systems"]), "files": tuple(dict(f) for f in d["files"]),
        "entry_points": tuple(_entry(e) for e in d["entry_points"]),
        "packages": tuple(PackageInfo(**p) for p in d["packages"]),
        "source_roots": tuple(d["source_roots"]), "test_roots": tuple(d["test_roots"]),
        "options": dict(d["options"]),
        "requirements": tuple(_requirement(r) for r in d["requirements"]),
        "dependencies": tuple(SourceRef(**r) for r in d["dependencies"]),
        "commands": tuple(_command(c) for c in d["commands"]),
        "sensitive": tuple(SensitiveFinding(**s) for s in d["sensitive"]),
        "risks": tuple(RiskSignal(**r) for r in d["risks"]),
        "not_inspected": tuple(d["not_inspected"]), "notes": tuple(d["notes"]),
    })


def validation_plan_from_dict(d: dict[str, Any]) -> ValidationPlan:
    return ValidationPlan(**{
        **d, "files": tuple(PlannedFile(**f) for f in d["files"]), "options": dict(d["options"]),
        "commands": tuple(tuple(c) for c in d["commands"]), "warnings": tuple(d["warnings"]),
        "created_at": datetime.fromisoformat(d["created_at"]),
    })


def validation_approval_from_dict(d: dict[str, Any]) -> ValidationApproval:
    return ValidationApproval(**{
        **d, "approved_at": datetime.fromisoformat(d["approved_at"]),
        "used_at": datetime.fromisoformat(d["used_at"]) if d.get("used_at") else None,
    })


def validation_result_from_dict(d: dict[str, Any]) -> ValidationResult:
    return ValidationResult(**{
        **d, "tool_versions": dict(d["tool_versions"]), "notes": tuple(d["notes"]),
        "steps": tuple(ValidationStep(**{**s, "command": tuple(s["command"]), "redactions": dict(s["redactions"])})
                       for s in d["steps"]),
        "finished_at": datetime.fromisoformat(d["finished_at"]),
    })


def local_findings_from_dict(d: dict[str, Any] | None) -> LocalFindings | None:
    if not d:
        return None
    return LocalFindings(
        inspection_id=d["inspection_id"], applied_at=datetime.fromisoformat(d["applied_at"]),
        entry_points=tuple(_entry(e) for e in d["entry_points"]),
        requirements=tuple(_requirement(r) for r in d["requirements"]),
        commands=tuple(_command(c) for c in d["commands"]),
        validations=tuple(validation_result_from_dict(v) for v in d.get("validations", ())),
        resolved_flags=tuple(d.get("resolved_flags", ())),
        added_flags=tuple(d.get("added_flags", ())),
        manual_review_reason=d.get("manual_review_reason"),
    )
