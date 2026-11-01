"""The local inspection workflow. Reads approved files only; never executes anything.

Language-agnostic: it reads what was approved through the guarded readers, runs the privacy
scan over every file it read, and hands the text to the inspector the registry chose. When no
inspector applies, the result says "manual review needed": no commands, no entry points and no
validation, and no assumptions about the project, while the privacy scan still applies.

``supported`` commands are backed by an inspected file; ``guess`` commands are not. Neither
means a command was run; only a recorded validation can say that.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Iterable

from ..grouping.models import CandidateGroup
from ..grouping.scoring import stable_id
from ..inventory.models import Inventory, SkipReason
from .build_files import project_folder
from .inspectors import DEFAULT_REGISTRY, MANUAL_REVIEW_REASON, InspectionInput, InspectorRegistry, InspectorReport, SourceFile
from .models import BuildFileProposal, BuildInspection, FileApprovalRecord, SensitiveFinding
from .reader import ApprovedFileReader, ReadRefused
from .selection import project_path
from .sensitive import MAX_FINDINGS_PER_FILE, scan_text


def _inventory_coverage(group: CandidateGroup, inventory: Inventory, proposal: BuildFileProposal, read_paths: set[str],
                        approved_ids: set[str], build_names: set[str]) -> tuple[list[str], list[str], list[SensitiveFinding]]:
    """What was not read: group files, everything including review-only files, and name-based flags."""
    records = {f.id: f for f in inventory.files}
    unapproved: list[str] = []
    findings: list[SensitiveFinding] = []
    for member in group.members:
        record = records.get(member.file_id)
        if record is None or member.file_id in approved_ids:
            continue
        path = project_path(record, group, inventory)
        if path in read_paths:
            continue
        unapproved.append(path)
        if record.category.value in ("data", "spreadsheet", "document", "archive", "presentation") \
                and path.rsplit("/", 1)[-1] not in build_names:
            findings.append(SensitiveFinding("unreviewed_file", "low", path, None,
                                             f"{record.category.value} file that was not read; check it before publishing"))
    not_inspected = list(unapproved)
    for path in proposal.review_only:
        not_inspected.append(path)
        findings.append(SensitiveFinding("possible_config_file", "medium", path, None,
                                         "a configuration file that often holds credentials; never read"))
    located = project_folder(group)
    if located:
        root_id, anchor = located
        prefix = anchor + "/" if anchor else ""
        for skipped in inventory.skipped:
            if skipped.root_id != root_id or not skipped.relative_path.startswith(prefix):
                continue
            path = skipped.relative_path[len(prefix):]
            if skipped.reason in (SkipReason.SENSITIVE_NAME, SkipReason.CREDENTIAL_LOCATION):
                findings.append(SensitiveFinding("credential_file", "high", path, None,
                                                 "a credential-looking file in the project; never read, keep it out of "
                                                 "any published copy"))
            elif skipped.reason is SkipReason.PRIVATE_FOLDER:
                findings.append(SensitiveFinding("private_folder", "medium", path, None,
                                                 "a private folder in the project, which can hold local paths; keep it "
                                                 "out of any published copy"))
    return unapproved, not_inspected, findings


def inspect_project(
    *,
    inventory_key: str,
    group: CandidateGroup,
    inventory: Inventory,
    proposal: BuildFileProposal,
    source_approval: FileApprovalRecord | None,
    source_reader: ApprovedFileReader | None,
    build_approval: FileApprovalRecord | None,
    build_reader: ApprovedFileReader | None,
    identity_terms: Iterable[str] = (),
    registry: InspectorRegistry = DEFAULT_REGISTRY,
    now: datetime | None = None,
) -> BuildInspection:
    """Read approved files through the guarded readers and ask the project's inspector about them."""
    inspector = registry.get(proposal.inspector)
    terms = list(identity_terms)
    file_status: list[dict[str, str]] = []
    sensitive: list[SensitiveFinding] = []
    truncated: list[str] = []
    read_paths: set[str] = set()
    approved_ids: set[str] = set()

    def read(approved, reader: ApprovedFileReader, approval_name: str) -> str | None:
        approved_ids.add(approved.file_id)
        try:
            text = reader.read_text(approved.file_id)
        except ReadRefused as exc:
            file_status.append({"project_path": approved.project_path, "approved_under": approval_name,
                                "status": f"not read: {exc}"})
            return None
        file_status.append({"project_path": approved.project_path, "approved_under": approval_name, "status": "read"})
        read_paths.add(approved.project_path)
        findings, cut = scan_text(approved.project_path, text, terms, inspector=inspector)
        sensitive.extend(findings)
        if cut:
            truncated.append(approved.project_path)
        return text

    build_texts: dict[str, str] = {}
    if build_approval and build_reader:
        for approved in build_approval.files:
            text = read(approved, build_reader, "build configuration approval")
            if text is not None:
                build_texts[approved.project_path] = text
    sources: list[SourceFile] = []
    if source_approval and source_reader:
        for approved in source_approval.files:
            text = read(approved, source_reader, "file approval (Gate 1)")
            if text is not None:
                sources.append(SourceFile(approved.file_id, approved.project_path, approved.language, text))

    build_names = {p.rsplit("/", 1)[-1] for p in inspector.build_files} if inspector else set()
    unapproved, not_inspected, name_findings = _inventory_coverage(group, inventory, proposal, read_paths, approved_ids,
                                                                   build_names)
    sensitive.extend(name_findings)
    if inspector:
        report = inspector.inspect(InspectionInput(build_texts, tuple(sources), tuple(unapproved), proposal.present))
    else:
        report = InspectorReport()
    notes = list(report.notes)
    if truncated:
        notes.append(f"{len(truncated)} file(s) reached the limit of {MAX_FINDINGS_PER_FILE} findings; "
                     "there may be more in them.")

    created = now or datetime.now(UTC)
    approvals = [a.id for a in (source_approval, build_approval) if a]
    return BuildInspection(
        id=stable_id("bi", inventory_key, group.id, created.isoformat(), *approvals),
        inventory_key=inventory_key, group_id=group.id, created_at=created,
        source_approval_id=source_approval.id if source_approval else None,
        build_approval_id=build_approval.id if build_approval else None,
        inspector=inspector.name if inspector else None,
        manual_review_reason=None if inspector else MANUAL_REVIEW_REASON,
        build_systems=tuple(report.build_systems), files=tuple(file_status), entry_points=tuple(report.entry_points),
        packages=tuple(report.packages), source_roots=tuple(report.source_roots), test_roots=tuple(report.test_roots),
        options=dict(report.options), requirements=tuple(report.requirements), dependencies=tuple(report.dependencies),
        commands=tuple(report.commands), sensitive=tuple(sensitive), risks=tuple(report.risks),
        not_inspected=tuple(not_inspected), notes=tuple(dict.fromkeys(notes)),
    )
