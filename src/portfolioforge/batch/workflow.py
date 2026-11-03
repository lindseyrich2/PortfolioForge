"""The batch queue: moves every candidate project as far as it can safely go by itself.

The queue holds only what the existing records cannot say: skips, errors and a few checkpoints.
A project's state is worked out from the existing approvals, packs, proposals and stages each
time, so an interrupted run resumes where it stopped and finished work is never repeated.

``advance`` only does local work the user has already allowed. It never prompts, never contacts an
AI provider or GitHub, and never runs project code. It stops at the next decision, and the
review collects those decisions across all projects.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from ..devtools.privacy_check import local_identity_terms
from ..drive.duplicates import load_concerns
from ..grouping.cli import _propose, resolve_inventory
from ..grouping.models import FlagKind, GroupingRun, GroupKind
from ..grouping.store import load_run
from ..inventory.models import Inventory, ScanMode
from ..paths import data_dir
from ..privacy import PrivacySettings, load_privacy_settings
from ..publishing import cli as pcli
from ..publishing import workflow as pw
from ..understanding.analysis import build_proposal
from ..understanding.build_files import MAX_BUILD_FILE_BYTES, project_facts, propose_build_files
from ..understanding.disclosure import build_manifest
from ..understanding.eligibility import assess_run
from ..understanding.extract import build_evidence_pack
from ..understanding.findings import NOT_SENT_WITH_FINDINGS, apply_inspection, build_augmented_pack
from ..understanding.inspection import inspect_project
from ..understanding.inspectors import DEFAULT_REGISTRY
from ..understanding.models import EligibilityStatus
from ..understanding.reader import ApprovedFileReader
from ..understanding.redact import redact
from ..understanding.store import (
    _write,
    group_dir,
    latest_build_approval,
    latest_file_approval,
    latest_inspection,
    latest_pack,
    list_proposals,
    list_validation_results,
    load_manifest,
    load_validation_plan,
    save_augmented_pack,
    save_inspection,
    save_manifest,
    save_pack,
    save_proposal,
    save_validation_plan,
)
from ..understanding.validation import ValidationUnavailable, build_plan, validation_status

QUEUE_VERSION = 1
# Waiting for a decision. Every state names what would move it on.
ACTIONS: dict[str, tuple[str, str]] = {
    "needs_attribution_review": ("exclude --reason TEXT, or defer", "review who owns this GitHub project. Its files aren't "
                                                                    "read from here; to analyze it, clone it and scan it locally"),
    "needs_read_approval": ("approve-read", "allow reading this project's files on this computer"),
    "needs_provider": ("run --provider anthropic", "choose an AI provider, so disclosures can be prepared"),
    "needs_disclosure_approval": ("send", "approve sending this exact disclosure to the AI provider"),
    "needs_send_retry": ("send --resend", "a send ended unclearly: it may have been processed or charged; decide "
                                          "whether to resend"),
    "needs_name": ("accept-names", "choose the repository name"),
    "needs_staging": ("stage", "state ownership and license, and confirm the files to publish"),
    "needs_findings_review": ("publishing review", "check the flagged lines, then fix or accept them"),
    "needs_publish_approval": ("publish", "approve creating this private repository"),
}
#: Set aside by you, without reading anything. `restore` undoes either one.
EXCLUDED, DEFERRED = "excluded", "deferred"


@dataclass
class Job:
    group_id: str
    title: str = ""
    priority: int = 0
    state: str = "new"
    detail: str = ""
    error: str | None = None
    set_aside: str | None = None
    """excluded | deferred, chosen by you."""
    set_aside_reason: str | None = None
    concerns: list[str] = field(default_factory=list)
    checkpoints: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""


@dataclass
class Queue:
    inventory_key: str
    jobs: dict[str, Job] = field(default_factory=dict)
    provider: str | None = None
    model: str | None = None

    def ordered(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: (-j.priority, j.group_id))


def queue_path(inventory_key: str) -> Path:
    return data_dir() / "batch" / inventory_key / "queue.json"


def load_queue(inventory_key: str) -> Queue:
    path = queue_path(inventory_key)
    if not path.is_file():
        return Queue(inventory_key)
    data = json.loads(path.read_text(encoding="utf-8"))  # unreadable: stop, and never overwrite it
    if data.get("version") != QUEUE_VERSION:
        raise ValueError("Unsupported batch queue version.")
    return Queue(inventory_key, {k: Job(**v) for k, v in data["jobs"].items()}, data.get("provider"), data.get("model"))


def save_queue(queue: Queue) -> None:
    body = {"version": QUEUE_VERSION, "provider": queue.provider, "model": queue.model,
            "jobs": {k: asdict(v) for k, v in queue.jobs.items()}}
    _write(queue_path(queue.inventory_key).parent, "queue.json", json.dumps(body, indent=2, ensure_ascii=False))


@dataclass
class Context:
    key: str
    inventory: Inventory
    run: GroupingRun
    privacy: PrivacySettings
    terms: set[str]
    eligibility: dict[str, Any]

    def refresh_grouping(self, run: GroupingRun) -> None:
        self.run = run
        self.eligibility = {e.group_id: e for e in assess_run(run, self.inventory)}


def load_context(ref: str = "latest") -> Context:
    key, inventory = resolve_inventory(ref)
    run = load_run(key) or _propose(key, inventory, None)
    return Context(key, inventory, run, load_privacy_settings(), local_identity_terms(),
                   {e.group_id: e for e in assess_run(run, inventory)})


# --- the queue's contents -----------------------------------------------------------------------------


def _priority(ctx: Context, group) -> int:
    """Likely engineering projects first. Only an order: it never decides anything."""
    e = ctx.eligibility[group.id]
    status = {EligibilityStatus.TECHNICAL_CANDIDATE: 3, EligibilityStatus.NEEDS_REVIEW: 2}.get(e.status, 0)
    kind = 1 if group.kind is GroupKind.PROJECT else 0
    return status * 10_000 + kind * 1_000 + min(e.code_files + e.notebook_files, 999)


def _concerns(ctx: Context, group_id: str, other_source: dict[str, list[str]]) -> list[str]:
    concerns = list(other_source.get(group_id, ()))
    for flag in ctx.run.flags:
        if flag.target == group_id and flag.kind is FlagKind.POSSIBLE_DUPLICATE:
            concerns.append("possible duplicate")
    for r in ctx.run.relationships:
        if group_id in (r.source, r.target) and r.status != "dismissed" and "duplicate" in r.kind.value:
            other = r.target if r.source == group_id else r.source
            concerns.append(f"possible duplicate of {other}")
    return list(dict.fromkeys(concerns))


def sync_queue(ctx: Context, queue: Queue) -> None:
    """Add new candidates and refresh their order. Existing checkpoints are kept."""
    current = {g.id: g for g in ctx.run.groups}
    other_source = load_concerns(ctx.key)
    for group in current.values():
        job = queue.jobs.setdefault(group.id, Job(group.id))
        job.title = group.user_title or group.working_title.text
        job.priority = _priority(ctx, group)
        job.concerns = _concerns(ctx, group.id, other_source)
    for job in queue.jobs.values():
        if job.group_id not in current:
            job.state, job.detail = "gone", ""


# --- working out each project's state, doing safe local work on the way -------------------------------


def _published(key: str, group_id: str) -> bool:
    path = group_dir(key, group_id) / "audit.jsonl"
    if not path.is_file():
        return False
    return any(json.loads(line).get("outcome") == "published"
               for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def latest_stage(key: str, group_id: str) -> pw.Stage | None:
    folder = group_dir(key, group_id)
    stages = [pw.load_stage(key, group_id, p.name[len("publish-stage-"):-len(".json")])
              for p in folder.glob("publish-stage-*.json")] if folder.is_dir() else []
    return max(stages, key=lambda s: s.created_at, default=None)


def current_proposal(key: str, group_id: str):
    proposals = list_proposals(key, group_id)
    accepted = [p for p in proposals if p.status == "accepted"]
    return (accepted or proposals or [None])[-1]


def inspector_for(ctx: Context, group):
    return DEFAULT_REGISTRY.select(project_facts(group, ctx.inventory))


def _inspection(ctx: Context, group, source, build):
    current = latest_inspection(ctx.key, group.id)
    if current and current.source_approval_id == source.id and \
            current.build_approval_id == (build.id if build else None):
        return current
    inspection = inspect_project(
        inventory_key=ctx.key, group=group, inventory=ctx.inventory,
        proposal=propose_build_files(group, ctx.inventory, ctx.privacy), source_approval=source,
        source_reader=ApprovedFileReader(source, ctx.privacy), build_approval=build,
        build_reader=ApprovedFileReader(build, ctx.privacy, MAX_BUILD_FILE_BYTES) if build else None,
        identity_terms=ctx.terms)
    save_inspection(inspection)
    return inspection


def _prepare_manifest(ctx: Context, queue: Queue, job: Job, inspection) -> None:
    """Ready the exact disclosure. Nothing is sent. Confirmed local facts are included in it."""
    existing = job.checkpoints.get("manifest_id")
    if existing:
        manifest, _ = load_manifest(ctx.key, job.group_id, existing)
        if (manifest.provider, manifest.model) == (queue.provider, queue.model):
            return
    pack = latest_pack(ctx.key, job.group_id)
    also_not_sent: tuple[str, ...] = ()
    if not inspection.manual_review_reason:
        pack = build_augmented_pack(pack, inspection)
        save_augmented_pack(pack)
        also_not_sent = NOT_SENT_WITH_FINDINGS
    manifest, payload = build_manifest(pack, queue.provider, queue.model, also_not_sent=also_not_sent)
    save_manifest(manifest, payload)
    job.checkpoints["manifest_id"] = manifest.id


def _recover_saved_answer(ctx: Context, job: Job):
    """An answer already paid for, saved before an interruption, becomes a proposal without a new request."""
    manifest_id = job.checkpoints.get("manifest_id")
    if not manifest_id:
        return None
    saved = sorted(group_dir(ctx.key, job.group_id).glob(f"response-{manifest_id}-*.json"))
    if not saved:
        return None
    manifest, payload = load_manifest(ctx.key, job.group_id, manifest_id)
    proposal = build_proposal(response_text=saved[-1].read_text(encoding="utf-8"), sent_body=json.loads(payload),
                              inventory_key=ctx.key, group_id=job.group_id, manifest_id=manifest.id,
                              provider=manifest.provider, model=manifest.model)
    save_proposal(proposal)
    job.checkpoints.pop("send_started", None)
    return proposal


def _prepare_plans(ctx: Context, job: Job, inspection, source) -> None:
    """Ready each offered check, so its commands and risks can be shown. Nothing is run."""
    inspector = DEFAULT_REGISTRY.get(inspection.inspector)
    plans, notes = job.checkpoints.setdefault("plans", {}), job.checkpoints.setdefault("plan_notes", {})
    if inspector is None:
        notes["all"] = "validation unavailable: no inspector for this project type"
        return
    finished = {r.action for r in list_validation_results(ctx.key, job.group_id) if r.inspection_id == inspection.id}
    for action in inspector.validation_actions():
        if action in finished:
            plans.pop(action, None)
            continue
        if action in plans and load_validation_plan(ctx.key, job.group_id, plans[action]).inspection_id == inspection.id:
            continue
        try:
            plan = build_plan(inspection=inspection, source_approval=source,
                              reader=ApprovedFileReader(source, ctx.privacy), action=action)
        except ValidationUnavailable as exc:
            plans.pop(action, None)
            notes[action] = str(exc)
            continue
        save_validation_plan(plan)
        plans[action] = plan.id
        notes.pop(action, None)


def _refreshed_findings(ctx: Context, proposal, inspection) -> Any:
    """Keep the proposal in step with the inspection and any checks that have run."""
    results = tuple(r for r in list_validation_results(ctx.key, proposal.group_id) if r.inspection_id == inspection.id)
    found = proposal.local_findings
    same = found and found.inspection_id == inspection.id and \
        {v.id for v in found.validations} == {r.id for r in results}
    if same or (proposal.status == "accepted" and not (found and found.inspection_id == inspection.id)):
        return proposal
    updated = apply_inspection(proposal, inspection, results)
    save_proposal(updated)
    return updated


def _stage_step(ctx: Context, job: Job, group, proposal):
    stage = latest_stage(ctx.key, group.id)
    if stage is None:
        return None
    try:
        pw.verify_tree(stage)
    except pw.PublishRefused:
        return None  # made under older rules, or changed: it needs staging again
    readme = hashlib.sha256(proposal.readme_markdown(draft=False, attribution=stage.attribution).encode("utf-8")).hexdigest()
    staged = next(f.sha256 for f in stage.files if f.path == "README.md")
    if readme != staged:  # new results: same files, same acceptances, new documents
        eligibility = ctx.eligibility[group.id]
        stage, _, _ = pw.refresh_stage(stage, group=group, eligibility=eligibility, proposal=proposal,
                                       inventory=ctx.inventory, privacy=ctx.privacy, identity_terms=ctx.terms,
                                       inspector=inspector_for(ctx, group))
    return stage


def _set(job: Job, state: str, detail: str = "") -> None:
    job.state, job.detail = state, detail


def advance(ctx: Context, queue: Queue, job: Job) -> None:
    """Do whatever is safe for one project, and stop at its next decision."""
    job.error = None
    job.updated_at = datetime.now(UTC).isoformat()
    group = ctx.run.group(job.group_id)
    if group is None:
        return _set(job, "gone")
    if _published(ctx.key, group.id):  # first: a published project stays recorded as published
        return _set(job, "published")
    if job.set_aside:  # before anything else, so nothing is read or prepared for it
        return _set(job, job.set_aside, job.set_aside_reason or "")
    eligibility = ctx.eligibility[group.id]
    if eligibility.status in (EligibilityStatus.PERSONAL_OR_ADMIN, EligibilityStatus.EXCLUDED):
        return _set(job, "not_eligible", "; ".join(eligibility.reasons))
    if ctx.inventory.report.mode is ScanMode.GITHUB:  # discovery only: nothing here can be read, sent or published
        return _set(job, "needs_attribution_review", eligibility.status.value)
    source = latest_file_approval(ctx.key, group.id)
    if source is None:
        return _set(job, "needs_read_approval", eligibility.status.value)

    if latest_pack(ctx.key, group.id) is None:
        save_pack(build_evidence_pack(source, ApprovedFileReader(source, ctx.privacy)))
    build = latest_build_approval(ctx.key, group.id)
    inspection = _inspection(ctx, group, source, build)

    proposal = current_proposal(ctx.key, group.id) or _recover_saved_answer(ctx, job)
    if proposal is None:
        if job.checkpoints.get("send_started"):  # never sent again without your decision
            return _set(job, "needs_send_retry", job.checkpoints.get("send_failure") or "the send was interrupted")
        if not queue.provider:
            return _set(job, "needs_provider")
        _prepare_manifest(ctx, queue, job, inspection)
        return _set(job, "needs_disclosure_approval")
    job.checkpoints.pop("send_started", None)

    proposal = _refreshed_findings(ctx, proposal, inspection)
    _prepare_plans(ctx, job, inspection, source)
    if proposal.status != "accepted":
        valid = sum(n.valid for n in proposal.names)
        return _set(job, "needs_name", "" if valid else "no valid suggested name: give your own")
    stage = _stage_step(ctx, job, group, proposal)
    if stage is None:
        return _set(job, "needs_staging")
    findings = pw.run_checks(stage, identity_terms=ctx.terms, inspector=inspector_for(ctx, group),
                             committer_email=pw.committer_email(stage, pcli.make_runner))
    blocking = pw.blockers(findings)
    if blocking:
        fixed = sum(not f.resolvable for f in blocking)
        return _set(job, "needs_findings_review", f"{len(blocking)} unresolved"
                    + (f", {fixed} can only be fixed, not accepted" if fixed else ""))
    return _set(job, "needs_publish_approval")


def advance_all(ctx: Context, queue: Queue, group_ids: Iterable[str] | None = None, limit: int | None = None) -> None:
    """One project failing never stops the others. Saved after each, so an interruption loses nothing."""
    wanted = set(group_ids) if group_ids is not None else None
    jobs = [j for j in queue.ordered() if wanted is None or j.group_id in wanted]
    for job in jobs[:limit] if limit else jobs:
        try:
            advance(ctx, queue, job)
        except Exception as exc:  # noqa: BLE001 - recorded on the job, and retried on the next resume
            job.state, job.error = "error", f"{type(exc).__name__}: {redact(str(exc))[0][:300]}"
        save_queue(queue)


# --- for the review ---------------------------------------------------------------------------------------


def project_summary(ctx: Context, job: Job) -> dict[str, Any]:
    group = ctx.run.group(job.group_id)
    proposal = current_proposal(ctx.key, job.group_id)
    inspection = latest_inspection(ctx.key, job.group_id)
    names = [n.slug for n in proposal.names if n.valid] if proposal else []
    name = proposal.accepted_name if proposal and proposal.accepted_name else (names[0] if names else None)
    privacy, status, stage_state = "not scanned yet", {}, None
    if inspection:
        high = sum(s.confidence == "high" for s in inspection.sensitive)
        privacy = f"{len(inspection.sensitive)} possible, {high} likely"
        status = validation_status(inspection, list_validation_results(ctx.key, job.group_id))
    if group is not None and job.state == "needs_publish_approval":
        stage_state = latest_stage(ctx.key, job.group_id)
    return {"job": job, "name": name, "names": names, "accepted": bool(proposal and proposal.accepted_name), "privacy": privacy,
            "validation": status, "stage": stage_state, "flags": len(proposal.review_flags) if proposal else 0,
            "risks": len(inspection.risks) if inspection else 0}
