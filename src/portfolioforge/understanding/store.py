"""Local storage for approvals, evidence packs, manifests and the audit log.

    <data dir>/understanding/<inventory id>/<group id>/
        file-approval-<id>.json   Gate 1 records
        evidence-<id>.json        redacted excerpts with source references
        manifest-<id>.json        what would be sent, and to whom
        payload-<id>.json         the exact payload bytes, for inspection
        disclosure-approval-<id>.json Gate 2 records
        response-<manifest>-<hash>.json   a provider's raw answer (local only)
        proposal-<id>.json        verified analysis and naming proposal, with your edits
        README-draft-<id>.md      draft README rendered from verified claims
        build-approval-<id>.json  approval to read build configuration files
        inspection-<id>.json      local build-and-run findings (no flagged values)
        augmented-pack-<id>.json  evidence plus confirmed findings, for a new manifest
        validation-plan-<id>.json, validation-approval-<id>.json, validation-result-<id>.json
        audit.jsonl               every send or validation attempt (no content)

Never inside a Git repository. Evidence and payloads contain excerpts of your
code: keep them private.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TypeVar

if TYPE_CHECKING:
    from .analysis import AnalysisProposal

from ..paths import data_dir, ensure_safe_output_dir
from .models import (
    BuildInspection,
    DisclosureManifest,
    EvidencePack,
    FileApprovalRecord,
    ManifestApproval,
    ValidationApproval,
    ValidationPlan,
    ValidationResult,
    approval_from_dict,
    dumps,
    inspection_from_dict,
    manifest_approval_from_dict,
    manifest_from_dict,
    pack_from_dict,
    to_jsonable,
    validation_approval_from_dict,
    validation_plan_from_dict,
    validation_result_from_dict,
)

T = TypeVar("T")


def _safe_part(value: str) -> str:
    if not value or any(c in value for c in "/\\:") or value in (".", ".."):
        raise ValueError("Invalid identifier.")
    return value


def group_dir(inventory_key: str, group_id: str) -> Path:
    return data_dir() / "understanding" / _safe_part(inventory_key) / _safe_part(group_id)


def _write(folder: Path, name: str, text: str | bytes) -> Path:
    folder = ensure_safe_output_dir(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    temporary = folder / (name + ".tmp")
    if isinstance(text, bytes):
        temporary.write_bytes(text)
    else:
        temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    return path


def _latest(folder: Path, prefix: str, loader: Callable[[dict[str, Any]], T]) -> T | None:
    if not folder.is_dir():
        return None
    candidates = sorted((p for p in folder.glob(f"{prefix}-*.json")), key=lambda p: p.stat().st_mtime)
    for path in reversed(candidates):
        return loader(json.loads(path.read_text(encoding="utf-8")))
    return None


def save_file_approval(record: FileApprovalRecord) -> Path:
    return _write(group_dir(record.inventory_key, record.group_id), f"file-approval-{record.id}.json", dumps(record))


def latest_file_approval(inventory_key: str, group_id: str) -> FileApprovalRecord | None:
    return _latest(group_dir(inventory_key, group_id), "file-approval", approval_from_dict)


def save_pack(pack: EvidencePack) -> Path:
    return _write(group_dir(pack.inventory_key, pack.group_id), f"evidence-{pack.id}.json", dumps(pack))


def latest_pack(inventory_key: str, group_id: str) -> EvidencePack | None:
    return _latest(group_dir(inventory_key, group_id), "evidence", pack_from_dict)


def save_augmented_pack(pack: EvidencePack) -> Path:
    """Kept apart from plain evidence packs, so findings enter a manifest only when asked for."""
    return _write(group_dir(pack.inventory_key, pack.group_id), f"augmented-pack-{pack.id}.json", dumps(pack))


def latest_augmented_pack(inventory_key: str, group_id: str) -> EvidencePack | None:
    return _latest(group_dir(inventory_key, group_id), "augmented-pack", pack_from_dict)


def save_build_approval(record: FileApprovalRecord) -> Path:
    """Stored under its own name, so Gate 1 and evidence extraction can never pick it up."""
    return _write(group_dir(record.inventory_key, record.group_id), f"build-approval-{record.id}.json", dumps(record))


def latest_build_approval(inventory_key: str, group_id: str) -> FileApprovalRecord | None:
    return _latest(group_dir(inventory_key, group_id), "build-approval", approval_from_dict)


def save_inspection(inspection: BuildInspection) -> Path:
    return _write(group_dir(inspection.inventory_key, inspection.group_id), f"inspection-{inspection.id}.json",
                  dumps(inspection))


def latest_inspection(inventory_key: str, group_id: str) -> BuildInspection | None:
    return _latest(group_dir(inventory_key, group_id), "inspection", inspection_from_dict)


def save_validation_plan(plan: ValidationPlan) -> Path:
    return _write(group_dir(plan.inventory_key, plan.group_id), f"validation-plan-{plan.id}.json", dumps(plan))


def load_validation_plan(inventory_key: str, group_id: str, plan_id: str) -> ValidationPlan:
    path = group_dir(inventory_key, group_id) / f"validation-plan-{_safe_part(plan_id)}.json"
    if not path.is_file():
        raise FileNotFoundError("No such validation plan.")
    return validation_plan_from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_validation_approval(approval: ValidationApproval, plan: ValidationPlan) -> Path:
    return _write(group_dir(plan.inventory_key, plan.group_id), f"validation-approval-{approval.id}.json",
                  dumps(approval))


def find_validation_approval(plan: ValidationPlan) -> ValidationApproval | None:
    """An approval for this plan that has not been used, if there is one."""
    folder = group_dir(plan.inventory_key, plan.group_id)
    if not folder.is_dir():
        return None
    for path in sorted(folder.glob("validation-approval-*.json")):
        approval = validation_approval_from_dict(json.loads(path.read_text(encoding="utf-8")))
        if approval.plan_id == plan.id and approval.used_at is None:
            return approval
    return None


def save_validation_result(result: ValidationResult) -> Path:
    return _write(group_dir(result.inventory_key, result.group_id), f"validation-result-{result.id}.json", dumps(result))


def list_validation_results(inventory_key: str, group_id: str) -> list[ValidationResult]:
    folder = group_dir(inventory_key, group_id)
    if not folder.is_dir():
        return []
    return sorted((validation_result_from_dict(json.loads(p.read_text(encoding="utf-8")))
                   for p in folder.glob("validation-result-*.json")), key=lambda r: r.finished_at)


def save_manifest(manifest: DisclosureManifest, payload: bytes) -> Path:
    folder = group_dir(manifest.inventory_key, manifest.group_id)
    _write(folder, f"payload-{manifest.id}.json", payload)
    return _write(folder, f"manifest-{manifest.id}.json", dumps(manifest))


def load_manifest(inventory_key: str, group_id: str, manifest_id: str) -> tuple[DisclosureManifest, bytes]:
    folder = group_dir(inventory_key, group_id)
    manifest = manifest_from_dict(json.loads((folder / f"manifest-{_safe_part(manifest_id)}.json")
                                             .read_text(encoding="utf-8")))
    return manifest, (folder / f"payload-{manifest_id}.json").read_bytes()


def latest_manifest(inventory_key: str, group_id: str) -> DisclosureManifest | None:
    return _latest(group_dir(inventory_key, group_id), "manifest", manifest_from_dict)


def save_manifest_approval(approval: ManifestApproval, manifest: DisclosureManifest) -> Path:
    return _write(group_dir(manifest.inventory_key, manifest.group_id),
                  f"disclosure-approval-{approval.id}.json", dumps(approval))


def find_manifest_approval(manifest: DisclosureManifest) -> ManifestApproval | None:
    folder = group_dir(manifest.inventory_key, manifest.group_id)
    if not folder.is_dir():
        return None
    for path in sorted(folder.glob("disclosure-approval-*.json")):
        approval = manifest_approval_from_dict(json.loads(path.read_text(encoding="utf-8")))
        if approval.manifest_id == manifest.id:
            return approval
    return None


def save_response(inventory_key: str, group_id: str, manifest_id: str, text: str) -> Path:
    """Keep the provider's raw answer locally, for inspection only."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return _write(group_dir(inventory_key, group_id), f"response-{_safe_part(manifest_id)}-{digest}.json", text)


def save_proposal(proposal: "AnalysisProposal") -> Path:
    return _write(group_dir(proposal.inventory_key, proposal.group_id), f"proposal-{proposal.id}.json",
                  dumps(proposal))


def load_proposal(inventory_key: str, group_id: str, proposal_id: str) -> "AnalysisProposal":
    from .analysis import proposal_from_dict
    path = group_dir(inventory_key, group_id) / f"proposal-{_safe_part(proposal_id)}.json"
    if not path.is_file():
        raise FileNotFoundError("No such proposal.")
    return proposal_from_dict(json.loads(path.read_text(encoding="utf-8")))


def list_proposals(inventory_key: str, group_id: str) -> list["AnalysisProposal"]:
    from .analysis import proposal_from_dict
    folder = group_dir(inventory_key, group_id)
    if not folder.is_dir():
        return []
    return sorted((proposal_from_dict(json.loads(p.read_text(encoding="utf-8")))
                   for p in folder.glob("proposal-*.json")), key=lambda p: p.created_at)


def accepted_names(inventory_key: str, *, except_group: str | None = None) -> set[str]:
    """Repository names already accepted for other projects (local collision check)."""
    base = data_dir() / "understanding" / _safe_part(inventory_key)
    if not base.is_dir():
        return set()
    from .analysis import proposal_from_dict
    names = set()
    for path in base.glob("*/proposal-*.json"):
        proposal = proposal_from_dict(json.loads(path.read_text(encoding="utf-8")))
        if proposal.accepted_name and proposal.group_id != except_group:
            names.add(proposal.accepted_name)
    return names


def save_readme(proposal: "AnalysisProposal") -> Path:
    return _write(group_dir(proposal.inventory_key, proposal.group_id), f"README-draft-{proposal.id}.md",
                  proposal.readme_markdown())


def append_audit(inventory_key: str, group_id: str, entry: dict[str, Any]) -> Path:
    folder = ensure_safe_output_dir(group_dir(inventory_key, group_id))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "audit.jsonl"
    record = {"at": datetime.now(UTC).isoformat(), **to_jsonable(entry)}
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path
