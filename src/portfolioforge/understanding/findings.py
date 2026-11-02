"""Using local inspection results: in the draft README, and optionally in a new disclosure.

Two separate routes:

* :func:`apply_inspection` updates a proposal *locally*. Nothing leaves the computer.
* :func:`build_augmented_pack` adds findings to an evidence pack for a *new* manifest.
  The payload changes, so its hash changes, and earlier approvals do not cover it: Gate 2
  applies again in full. Only confirmed facts go in. Guesses are left out so a model cannot
  cite one as evidence, and so are the sensitive-data scan, risk signals and program output.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime

from ..grouping.scoring import stable_id
from .analysis import AnalysisProposal, ProposalError
from .models import BuildInspection, EvidenceItem, EvidencePack, LocalFindings, ValidationResult
from .redact import redact

FINDINGS_PATH = "local-inspection"
FINDINGS_LANGUAGE = "Build configuration"
FINDINGS_KIND = "local_finding"
NOT_SENT_WITH_FINDINGS = (
    "commands labelled as guesses",
    "the credential and personal-data scan results",
    "risk signals, and any output or result of a local validation run",
)
_FLAG_PREFIX = "local inspection: "
#: Earlier review flags that a supported command settles.
_SETTLED_BY_RUN = ("no verified run instructions", "run/test instructions need an additional approved local inspection")
_SETTLED_BY_TEST = ("no test instructions",)


def inspection_flags(inspection: BuildInspection) -> list[str]:
    flags: list[str] = []
    if inspection.manual_review_reason:
        flags.append(f"{_FLAG_PREFIX}manual review needed: {inspection.manual_review_reason}")
    high = sum(s.confidence == "high" for s in inspection.sensitive)
    others = len(inspection.sensitive) - high
    if high:
        flags.append(f"{_FLAG_PREFIX}{high} likely credential or identifier finding(s): resolve before publishing")
    if others:
        flags.append(f"{_FLAG_PREFIX}{others} possible personal-data or identity finding(s): review before publishing")
    if inspection.not_inspected:
        flags.append(f"{_FLAG_PREFIX}{len(inspection.not_inspected)} project file(s) were not read, so they were not "
                     "scanned: check them before publishing")
    if not inspection.manual_review_reason and not inspection.commands_for("run", "supported"):
        flags.append(f"{_FLAG_PREFIX}no run command is supported by the inspected files")
    if any(c.status == "guess" for c in inspection.commands):
        flags.append(f"{_FLAG_PREFIX}some commands are guesses and labelled as such in the README")
    if any(not p.location_matches for p in inspection.packages):
        flags.append(f"{_FLAG_PREFIX}some files are not in the folder their package declaration requires")
    if any(e.status == "declared_only" for e in inspection.entry_points):
        flags.append(f"{_FLAG_PREFIX}a declared main class could not be confirmed in the inspected source")
    return flags


def apply_inspection(proposal: AnalysisProposal, inspection: BuildInspection,
                     validations: tuple[ValidationResult, ...] = (), now: datetime | None = None) -> AnalysisProposal:
    """Add local findings to a proposal. Local only; calling this again replaces them.

    An accepted proposal keeps its name, status and findings; it can only take new validation
    results for the inspection it already uses, so what was observed can still reach the README.
    """
    if (proposal.inventory_key, proposal.group_id) != (inspection.inventory_key, inspection.group_id):
        raise ProposalError("This inspection belongs to a different project.")
    previous = proposal.local_findings
    if proposal.status == "accepted":
        if previous is None or previous.inspection_id != inspection.id:
            raise ProposalError("This proposal is already accepted; apply findings before accepting it.")
        return replace(proposal, local_findings=replace(
            previous, validations=tuple(v for v in validations if v.inspection_id == inspection.id)))
    flags = list(proposal.review_flags)
    if previous:  # back out what the earlier inspection did
        flags = [f for f in flags if f not in previous.added_flags] + list(previous.resolved_flags)
    settled = set()
    if inspection.commands_for("run", "supported"):
        settled.update(_SETTLED_BY_RUN)
    if inspection.commands_for("test", "supported"):
        settled.update(_SETTLED_BY_TEST)
    resolved = tuple(f for f in flags if f in settled)
    added = tuple(inspection_flags(inspection))
    when = now or datetime.now(UTC)
    current = tuple(v for v in validations if v.inspection_id == inspection.id)
    findings = LocalFindings(
        inspection_id=inspection.id, applied_at=when, entry_points=inspection.entry_points,
        requirements=inspection.requirements, commands=inspection.commands, validations=current,
        resolved_flags=resolved, added_flags=added, manual_review_reason=inspection.manual_review_reason,
    )
    return replace(
        proposal, status="edited", local_findings=findings,
        review_flags=tuple(f for f in flags if f not in settled) + added,
        edits=(*proposal.edits, {"field": "local_inspection", "value": inspection.id, "at": when.isoformat()}),
    )


def _finding_texts(inspection: BuildInspection) -> list[str]:
    texts: list[str] = []
    if inspection.build_systems:
        texts.append("Build systems found in inspected files: " + ", ".join(inspection.build_systems) + ".")
    for entry in inspection.entry_points:
        if entry.status == "declared_only":
            continue
        where = f" (found at {entry.source.location})" if entry.source else ""
        declared = "; declared in " + ", ".join(r.location for r in entry.declared_by) if entry.declared_by else ""
        texts.append(f"Entry point: {entry.name}{where}{declared}.")
    for package in inspection.packages:
        texts.append(f"Package {package.name or '(default package)'}: {package.files} file(s).")
    for requirement in inspection.requirements:
        texts.append(f"Requirement: {requirement.setting}={requirement.value} ({requirement.source.location}).")
    for command in inspection.commands:
        if command.status != "supported":
            continue
        where = f" (from folder {command.working_dir})" if command.working_dir else ""
        texts.append(f"Command supported by inspected files, not run, for {command.purpose}{where}: "
                     f"{command.command} . Basis: {command.reason}.")
    return texts


def build_augmented_pack(pack: EvidencePack, inspection: BuildInspection, now: datetime | None = None) -> EvidencePack:
    """An evidence pack with confirmed findings first, so the payload limit cannot drop them."""
    if (pack.inventory_key, pack.group_id) != (inspection.inventory_key, inspection.group_id):
        raise ValueError("This inspection belongs to a different project.")
    items = []
    for number, text in enumerate(_finding_texts(inspection), start=1):
        clean, counts = redact(text)
        items.append(EvidenceItem(
            id=stable_id("e", inspection.id, FINDINGS_KIND, str(number)), file_id=inspection.id,
            project_path=FINDINGS_PATH, language=FINDINGS_LANGUAGE, kind=FINDINGS_KIND, line_start=number,
            line_end=number, cell=None, text=clean, sha256=hashlib.sha256(clean.encode("utf-8")).hexdigest(),
            redactions=counts,
        ))
    created = now or datetime.now(UTC)
    return replace(
        pack, id=stable_id("p", pack.id, inspection.id), created_at=created, items=(*items, *pack.items),
        files=(*pack.files, {"project_path": FINDINGS_PATH, "language": FINDINGS_LANGUAGE,
                             "evidence_items": len(items), "status": "from local inspection"}),
    )
