"""Command line: ``python -m portfolioforge.batch``. One review for many projects.

Safe, and never asks anything:
    run [--provider anthropic] [--model M] [--limit N]   find candidates and prepare all that can be prepared
    resume                                               carry on after an interruption; retries errors
    status                                               one line per project
    review [--project GROUP]                             everything that needs you, in one place

Your decisions. Each one lists exactly what it covers, then asks you to type a confirmation:
    approve-read GROUP ... [--acknowledge-review]        allow reading these projects' files locally
    send GROUP ... [--resend]                            send these exact disclosures to the AI provider
    accept-names GROUP[=SLUG] ...                        choose repository names; the first valid suggestion is the default
    validate GROUP ... --action {compile,run,test}       run these checks on temporary copies
    stage GROUP ... --ownership O --license L [--coursework] [--attribution TEXT]
    publish GROUP ...                                    create these PRIVATE repositories under one approval of the listed scope

Set projects aside at any time, without reading their files or sending anything:
    exclude GROUP ... --reason TEXT                      leave out, such as unrelated work or a duplicate
    defer GROUP ... [--reason TEXT]                      not now
    restore GROUP ...                                    bring back into the batch

Nothing here sends project content, runs project code or publishes without that confirmation.
This workflow only creates private repositories. Existing repositories are never touched.
"""

from __future__ import annotations

import argparse
import re
import sys
from argparse import Namespace
from typing import Callable, Sequence

from ..grouping.cli import UsageError, _propose
from ..grouping.models import GroupStatus
from ..grouping.review import append_decision, decisions_path
from ..inventory.cli import format_error
from ..privacy import PrivacyConfigError
from ..publishing import cli as pcli
from ..publishing import workflow as pw
from ..understanding import cli as ucli
from ..understanding.analysis import DEFAULT_ANTHROPIC_MODEL, ProposalError, accept_proposal, edit_proposal
from ..understanding.build_files import approve_build_files, propose_build_files
from ..understanding.disclosure import (
    DisclosureNotApproved,
    ProviderNotConfigured,
    approve_manifest,
    format_manifest,
)
from ..understanding.models import EligibilityStatus
from ..understanding.selection import ApprovalError, approve_files, propose_selection
from ..understanding.store import (
    accepted_names,
    append_audit,
    find_manifest_approval,
    latest_build_approval,
    latest_file_approval,
    load_manifest,
    load_validation_plan,
    save_build_approval,
    save_file_approval,
    save_manifest_approval,
    save_proposal,
    save_validation_approval,
    save_validation_plan,
)
from ..understanding.validation import ACTIONS as CHECKS
from ..understanding.validation import ValidationRefused, ValidationUnavailable, approve_plan, build_plan, format_plan
from . import workflow as bw

EXIT_OK = 0
EXIT_ERROR = 1
Input = Callable[[str], str]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m portfolioforge.batch", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inventory", default="latest")
    parser.add_argument("--redact", action="store_true", help="show group IDs instead of names and paths")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="find candidates and prepare all that can be prepared; never asks anything")
    run.add_argument("--provider", choices=("anthropic",), help="prepare disclosures for this provider; sends nothing")
    run.add_argument("--model", help=f"default {DEFAULT_ANTHROPIC_MODEL}")
    run.add_argument("--limit", type=int, help="only work on the first N projects this time")
    sub.add_parser("resume", help="carry on after an interruption; repeats nothing that succeeded")
    sub.add_parser("status", help="one line per project")
    sub.add_parser("review", help="everything that needs you, in one place").add_argument("--project", metavar="GROUP")

    read = sub.add_parser("approve-read", help="allow reading these projects' files on this computer")
    read.add_argument("groups", nargs="+")
    read.add_argument("--acknowledge-review", action="store_true", help="needed for projects marked needs_review")
    send = sub.add_parser("send", help="send these exact disclosures to the AI provider")
    send.add_argument("groups", nargs="+")
    send.add_argument("--resend", action="store_true", help="resend after an interrupted send, which may be charged again")
    names = sub.add_parser("accept-names", help="choose names: GROUP takes the first valid suggestion, GROUP=SLUG yours")
    names.add_argument("groups", nargs="+")
    check = sub.add_parser("validate", help="run these checks on temporary copies")
    check.add_argument("groups", nargs="+")
    check.add_argument("--action", required=True, choices=CHECKS)
    stage = sub.add_parser("stage", help="prepare these projects for private publication")
    stage.add_argument("groups", nargs="+")
    stage.add_argument("--ownership", required=True, choices=pw.OWNERSHIP)
    stage.add_argument("--license", required=True, metavar="SPDX|none")
    stage.add_argument("--coursework", action="store_true", help="record in PROVENANCE.md that these began as coursework")
    stage.add_argument("--attribution", help="credit for other people's code; goes in PROVENANCE.md")
    publish = sub.add_parser("publish", help="create these PRIVATE repositories under one approval of the listed scope")
    publish.add_argument("groups", nargs="+")
    exclude = sub.add_parser("exclude", help="leave out, without reading or sending anything")
    exclude.add_argument("groups", nargs="+")
    exclude.add_argument("--reason", required=True)
    defer = sub.add_parser("defer", help="not now, without reading or sending anything")
    defer.add_argument("groups", nargs="+")
    defer.add_argument("--reason")
    sub.add_parser("restore", help="bring set-aside projects back into the batch").add_argument("groups", nargs="+")
    return parser


def main(argv: Sequence[str] | None = None, *, input_fn: Input = input) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # some Windows consoles can't print every character
    args = build_parser().parse_args(argv)
    try:
        return _run(args, input_fn)
    except (UsageError, ApprovalError, ProposalError, pw.PublishRefused, ValidationRefused, ValidationUnavailable,
            DisclosureNotApproved, ProviderNotConfigured) as exc:
        print(f"error: {exc}", file=sys.stderr)  # these messages never contain paths
        return EXIT_ERROR
    except (ValueError, OSError, PrivacyConfigError) as exc:
        print(format_error(exc, redact=args.redact), file=sys.stderr)
        return EXIT_ERROR


def _run(args: argparse.Namespace, input_fn: Input) -> int:
    ctx = bw.load_context(args.inventory)
    queue = bw.load_queue(ctx.key)
    bw.sync_queue(ctx, queue)
    if args.command in ("run", "resume"):
        if args.command == "run" and args.provider:
            queue.provider, queue.model = args.provider, args.model or DEFAULT_ANTHROPIC_MODEL
        bw.advance_all(ctx, queue, limit=getattr(args, "limit", None))
        print(_counts(queue))
        print("\nNothing was sent to an AI provider, run, or published. See what needs you with: review")
        return EXIT_OK
    if args.command == "status":
        print(_counts(queue))
        print()
        for job in queue.ordered():
            label = job.group_id if args.redact else f"{job.title} [{job.group_id}]"
            print(f"  {job.state:<26} {label}" + (f"  ERROR: {job.error}" if job.error else ""))
        return EXIT_OK
    if args.command == "review":
        print(_review(ctx, queue, args))
        return EXIT_OK
    if args.command in ("exclude", "defer", "restore"):
        return _set_aside(args, ctx, queue)
    handlers = {"approve-read": _approve_read, "send": _send, "accept-names": _accept_names, "validate": _validate,
                "stage": _stage, "publish": _publish}
    groups = [g.partition("=")[0] for g in args.groups]
    unknown = [g for g in groups if g not in queue.jobs]
    if unknown:
        raise UsageError(f"Not in this batch: {', '.join(unknown)}")
    code = handlers[args.command](args, ctx, queue, input_fn)
    bw.advance_all(ctx, queue, groups)
    print("\n" + _counts(queue))
    return code


def _ask(input_fn: Input, word: str) -> bool:
    try:  # refused when nobody is there to type
        return input_fn(f"\nType '{word}' to continue: ").strip() == word
    except EOFError:
        return False


def _in_state(queue: bw.Queue, groups: Sequence[str], *states: str) -> None:
    wrong = [g for g in groups if queue.jobs[g].state not in states]
    if wrong:
        raise UsageError("Not at this step (see status): " + ", ".join(f"{g} is {queue.jobs[g].state}" for g in wrong))


# --- what the review shows ---------------------------------------------------------------------------------


def _counts(queue: bw.Queue) -> str:
    states: dict[str, int] = {}
    for job in queue.jobs.values():
        states[job.state] = states.get(job.state, 0) + 1
    waiting = sum(n for s, n in states.items() if s in bw.ACTIONS)
    done = states.get("published", 0)
    errors = states.get("error", 0)
    return (f"Batch: {len(queue.jobs)} candidate(s): {waiting} waiting for you, {done} published, {errors} with errors, "
            f"{len(queue.jobs) - waiting - done - errors} set aside.")


def _review(ctx: bw.Context, queue: bw.Queue, args: argparse.Namespace) -> str:
    jobs = [j for j in queue.ordered() if not args.project or j.group_id == args.project]
    active = [j for j in jobs if j.state in bw.ACTIONS or j.state == "error"]
    lines = [_counts(queue), ""]
    for job in active:
        s = bw.project_summary(ctx, job)
        lines.append(job.group_id if args.redact else f"{job.title}  [{job.group_id}]")
        chosen = "chosen" if s["accepted"] else "proposed"
        if job.state == "needs_name" and not s["names"]:
            chosen = "none valid"
        lines.append(f"  Repository:  {'[hidden]' if args.redact else (s['name'] or 'none yet')}"
                     + (f" ({chosen})" if s["name"] else ""))
        if job.state == "needs_name" and s["names"]:
            shown = "[hidden]" if args.redact else (f"{s['names'][0]} (default)" + "".join(f", {n}" for n in s["names"][1:]))
            lines.append(f"  Name choices: {shown}. To choose another: GROUP=NAME")
        lines.append(f"  Readiness:   {job.state}" + (f": {job.detail}" if job.detail else ""))
        if s["validation"]:
            lines.append("  Validation:  " + "; ".join(f"{a} {t.split(':')[0]}" for a, t in s["validation"].items()))
        notes = job.checkpoints.get("plan_notes") or {}
        for action, note in notes.items():
            lines.append(f"    {action}: {note}")
        plans = job.checkpoints.get("plans") or {}
        if plans:
            lines.append(f"  Plans ready: {', '.join(plans)} (nothing has run; {s['risks']} risk signal(s) in the code)")
        lines.append(f"  Privacy:     {s['privacy']}")
        if s["flags"]:
            lines.append(f"  Review flags: {s['flags']}")
        for concern in job.concerns:
            lines.append(f"  Duplicate:   {concern}")
        if job.error:
            lines.append(f"  ERROR:       {job.error}  (resume retries this)")
        lines.append("")
    waiting: dict[str, list[str]] = {}
    for job in active:
        if job.state in bw.ACTIONS:
            waiting.setdefault(job.state, []).append(job.group_id)
    if waiting:
        lines.append("Decisions needed:")
        for state, (command, meaning) in bw.ACTIONS.items():
            if state in waiting:
                lines.append(f"  {meaning}:")
                lines.append(f"    {command} {' '.join(waiting[state])}")
    elif not active:
        lines.append("Nothing needs you right now.")
    aside = [j for j in jobs if j.state in (bw.EXCLUDED, bw.DEFERRED)]
    if aside:
        lines += ["", "Set aside (restore brings them back):"]
        lines += [f"  {j.state}: " + (j.group_id if args.redact else f"{j.title} [{j.group_id}]") for j in aside]
    if waiting.get("needs_read_approval"):
        lines += ["", "To leave any out without reading them: exclude GROUP ... --reason TEXT, or defer GROUP ..."]
    return "\n".join(lines).rstrip()


# --- decisions -----------------------------------------------------------------------------------------------


def _set_aside(args: argparse.Namespace, ctx: bw.Context, queue: bw.Queue) -> int:
    """Only changes the queue. Reads no project file, sends nothing, and leaves existing records alone."""
    unknown = [g for g in args.groups if g not in queue.jobs]
    if unknown:
        raise UsageError(f"Not in this batch: {', '.join(unknown)}")
    if args.command == "exclude" and not args.reason.strip():
        raise UsageError("Give a reason with --reason.")
    for group_id in args.groups:
        job = queue.jobs[group_id]
        job.set_aside = None if args.command == "restore" else (bw.EXCLUDED if args.command == "exclude" else bw.DEFERRED)
        job.set_aside_reason = None if args.command == "restore" else (args.reason or "").strip() or None
        if args.command == "restore":
            # A restored project picks up exactly where its records say it is; a project not
            # yet approved for reading stays unread.
            bw.advance_all(ctx, queue, [group_id])
        else:
            job.state, job.detail = job.set_aside, job.set_aside_reason or ""
            bw.save_queue(queue)
        print(f"{group_id}: {job.state}")
    return EXIT_OK


def _approve_read(args: argparse.Namespace, ctx: bw.Context, queue: bw.Queue, input_fn: Input) -> int:
    _in_state(queue, args.groups, "needs_read_approval")
    held = [g for g in args.groups
            if ctx.eligibility[g].status is EligibilityStatus.NEEDS_REVIEW and not args.acknowledge_review]
    if held:
        raise UsageError("These are marked needs_review; check them, then add --acknowledge-review: " + ", ".join(held))
    for group_id in args.groups:
        group = ctx.run.group(group_id)
        selection = propose_selection(group, ctx.inventory, ctx.privacy)
        build = propose_build_files(group, ctx.inventory, ctx.privacy)
        print(group_id if args.redact else f"{queue.jobs[group_id].title} [{group_id}]")
        for path in ([] if args.redact else [*(s.project_path for s in selection.selected),
                                             *(c.project_path for c in build.candidates)]):
            print(f"  {path}")
        print(f"  {len(selection.selected)} source file(s), {len(build.candidates)} build file(s); "
              f"{len(selection.excluded)} excluded")
    print("\nThese files would be read on this computer only. Nothing is sent, run or changed.")
    if not _ask(input_fn, f"read {len(args.groups)}"):
        print("Not approved. Nothing was read.")
        return EXIT_ERROR
    unaccepted = [g for g in args.groups if ctx.run.group(g).status is not GroupStatus.ACCEPTED]
    for group_id in unaccepted:
        append_decision(decisions_path(ctx.key), "accept", (group_id,))
    if unaccepted:
        ctx.refresh_grouping(_propose(ctx.key, ctx.inventory, None))
    for group_id in args.groups:
        group = ctx.run.group(group_id)
        record = approve_files(inventory_key=ctx.key, group=group, eligibility=ctx.eligibility[group_id],
                               proposal=propose_selection(group, ctx.inventory, ctx.privacy), inventory=ctx.inventory,
                               acknowledge_review=args.acknowledge_review)
        save_file_approval(record)
        build = propose_build_files(group, ctx.inventory, ctx.privacy)
        if build.candidates:
            save_build_approval(approve_build_files(inventory_key=ctx.key, group=group, proposal=build,
                                                    eligibility=ctx.eligibility[group_id],
                                                    acknowledge_review=args.acknowledge_review))
    print(f"Approved reading for {len(args.groups)} project(s).")
    return EXIT_OK


def _send(args: argparse.Namespace, ctx: bw.Context, queue: bw.Queue, input_fn: Input) -> int:
    """Consolidated Gate 2. Each manifest keeps its own approval, payload hash, provider and model."""
    states = ("needs_send_retry",) if args.resend else ("needs_disclosure_approval",)
    _in_state(queue, args.groups, *states)
    if args.resend:  # the interrupted disclosure needs its manifest again
        missing = [g for g in args.groups if not queue.jobs[g].checkpoints.get("manifest_id")]
        if missing:
            raise UsageError("No disclosure to resend for: " + ", ".join(missing))
    manifests = {}
    total_bytes, total_cost = 0, 0.0
    for group_id in args.groups:
        manifest, payload = load_manifest(ctx.key, group_id, queue.jobs[group_id].checkpoints["manifest_id"])
        manifests[group_id] = (manifest, payload)
        print(format_manifest(manifest, redact=args.redact))
        print()
        total_bytes += manifest.payload_bytes
        total_cost += (manifest.request.get("cost") or {}).get("max_cost_usd", 0.0)
    providers = sorted({f"{m.provider} / {m.model}" for m, _ in manifests.values()})
    print(f"These {len(manifests)} disclosures, {total_bytes:,} bytes in all, would leave this computer for "
          f"{', '.join(providers)}. The provider's data-retention policy applies. Estimated cost: at most about "
          f"${total_cost:.2f}, an upper bound, not an exact charge.")
    if args.resend:
        print("Warning: an earlier send of these was interrupted, so you may be charged again.")
    word = f"send {len(manifests)} to {manifests[args.groups[0]][0].provider}"
    if not _ask(input_fn, word):
        print("Not approved. Nothing was sent.")
        return EXIT_ERROR
    failed = 0
    for group_id, (manifest, _) in manifests.items():
        job = queue.jobs[group_id]
        approval = find_manifest_approval(manifest)
        if approval is None:
            save_manifest_approval(approve_manifest(manifest), manifest)
        job.checkpoints["send_started"] = manifest.id
        bw.save_queue(queue)  # first, so an interruption can never lead to a silent second charge
        try:
            code = ucli._submit(Namespace(manifest=manifest.id, yes=True), ctx.key, ctx.run.group(group_id), input_fn)
        except DisclosureNotApproved as exc:  # refused before sending
            print(f"error: {group_id}: {exc}", file=sys.stderr)
            job.checkpoints.pop("send_started", None)
            failed += 1
        except ProviderNotConfigured as exc:
            print(f"error: {group_id}: {exc}", file=sys.stderr)
            job.checkpoints.pop("send_started", None)
            failed += 1
        except UsageError as exc:
            print(f"error: {group_id}: {exc}", file=sys.stderr)
            failed += 1
            if _clearly_not_processed(str(exc)):
                job.checkpoints.pop("send_started", None)
            else:  # it may have been processed or charged: only you decide whether to send it again
                job.checkpoints["send_failure"] = "the request failed in a way that doesn't show whether it was processed"
        else:
            failed += code != EXIT_OK
            if code == EXIT_OK:
                job.checkpoints.pop("send_started", None)
                job.checkpoints.pop("send_failure", None)
            else:  # the provider answered, but with nothing usable: it was probably charged
                job.checkpoints["send_failure"] = "the provider answered without a usable result, and may have charged for it"
        bw.save_queue(queue)
    return EXIT_ERROR if failed else EXIT_OK


def _clearly_not_processed(message: str) -> bool:
    """Only when the failure itself shows the request was never accepted. Anything else is treated as maybe charged."""
    return "nothing was sent" in message or bool(re.search(r"\(HTTP 4(?!08)\d\d\b", message))


def _accept_names(args: argparse.Namespace, ctx: bw.Context, queue: bw.Queue, input_fn: Input) -> int:
    pairs = [(g.partition("=")[0], g.partition("=")[2]) for g in args.groups]
    _in_state(queue, [g for g, _ in pairs], "needs_name")
    chosen = []
    for group_id, slug in pairs:
        proposal = bw.current_proposal(ctx.key, group_id)
        name = slug or next((n.slug for n in proposal.names if n.valid), None)
        if name is None:
            raise UsageError(f"{group_id} has no valid suggested name. Give your own as {group_id}=my-name.")
        chosen.append((group_id, proposal, name))
        others = [n.slug for n in proposal.names if n.valid and n.slug != name]
        print(f"{'[hidden]' if args.redact else name}  for {group_id}  ({len(proposal.review_flags)} review flag(s))"
              + (f"; other choices: {', '.join(others)}" if others and not args.redact else ""))
        if not args.redact:
            print("\n".join(f"    - {flag}" for flag in proposal.review_flags))
    if len({name for *_, name in chosen}) != len(chosen):
        raise UsageError("Two projects would get the same repository name.")
    print("\nAccepting a name also acknowledges the review flags listed. Nothing is created on GitHub.")
    if not _ask(input_fn, f"accept {len(chosen)}"):
        print("Not accepted.")
        return EXIT_ERROR
    for group_id, proposal, name in chosen:
        if name not in {n.slug for n in proposal.names}:  # your own name is checked by the same rules
            proposal = edit_proposal(proposal, add_name=name)
        save_proposal(accept_proposal(proposal, name, acknowledge_flags=True,
                                      taken_names=accepted_names(ctx.key, except_group=group_id)))
    print(f"Accepted {len(chosen)} name(s).")
    return EXIT_OK


def _validate(args: argparse.Namespace, ctx: bw.Context, queue: bw.Queue, input_fn: Input) -> int:
    plans = []
    for group_id in args.groups:
        job = queue.jobs[group_id]
        plan_id = (job.checkpoints.get("plans") or {}).get(args.action)
        if plan_id:
            plan = load_validation_plan(ctx.key, group_id, plan_id)
        else:  # a repeat of a finished check gets a new plan, which needs its own approval
            inspection = bw.latest_inspection(ctx.key, group_id)
            approval = latest_file_approval(ctx.key, group_id)
            if inspection is None or approval is None:
                raise UsageError(f"{group_id} has not been inspected yet.")
            plan = build_plan(inspection=inspection, source_approval=approval, action=args.action,
                              reader=ucli.ApprovedFileReader(approval, ctx.privacy))
            save_validation_plan(plan)
        plans.append((group_id, plan))
        print(group_id)
        print(format_plan(plan, redact_names=args.redact))
        print()
    runs_code = any(plan.runs_project_code for _, plan in plans)
    word = f"run my code {len(plans)}" if runs_code else f"{args.action} {len(plans)}"
    if not _ask(input_fn, word):
        print("Not approved. Nothing was run.")
        return EXIT_ERROR
    failures = 0
    for group_id, plan in plans:
        save_validation_approval(approve_plan(plan), plan)
        append_audit(ctx.key, group_id, {"plan_id": plan.id, "action": plan.action, "plan_sha256": plan.plan_sha256,
                                         "outcome": "validation_approved"})
        print(group_id)
        try:  # always redacted: a program's output may hold sample data, and stays in the local record
            failures += ucli._validate(Namespace(plan=plan.id, redact=True), ctx.key, ctx.run.group(group_id),
                                       ctx.privacy) != EXIT_OK
        except ValidationRefused as exc:
            print(f"error: {group_id}: {exc}", file=sys.stderr)
            failures += 1
        (queue.jobs[group_id].checkpoints.get("plans") or {}).pop(args.action, None)
        bw.save_queue(queue)
    return EXIT_ERROR if failures else EXIT_OK


def _selected_for_publication(ctx: bw.Context, group_id: str) -> list[pw.Candidate]:
    """The files you already approved for reading, shown again before staging. Never the whole folder."""
    approved = {f.project_path for a in (latest_file_approval(ctx.key, group_id), latest_build_approval(ctx.key, group_id))
                if a for f in a.files}
    generated = {g.casefold() for g in pw.GENERATED}
    candidates, _ = pw.list_candidates(ctx.run.group(group_id), ctx.inventory, ctx.privacy, ctx.key)
    return [c for c in candidates if c.project_path in approved and c.project_path.casefold() not in generated]


def _stage(args: argparse.Namespace, ctx: bw.Context, queue: bw.Queue, input_fn: Input) -> int:
    _in_state(queue, args.groups, "needs_staging")
    selections = {}
    for group_id in args.groups:
        selections[group_id] = _selected_for_publication(ctx, group_id)
        print(f"{group_id}: {len(selections[group_id])} file(s) to publish, unchanged")
        if not args.redact:
            print("\n".join(f"  {c.project_path}" for c in selections[group_id]))
    statement = f"ownership {args.ownership}, license {args.license}" + (", coursework" if args.coursework else "")
    print(f"\nFor all of these you state: {statement}. These go in PROVENANCE.md. Staging is local, and PRIVATE.")
    if not _ask(input_fn, f"stage {len(selections)}"):
        print("Not staged.")
        return EXIT_ERROR
    for group_id, chosen in selections.items():
        stage = pw.create_stage(
            inventory_key=ctx.key, group=ctx.run.group(group_id), eligibility=ctx.eligibility[group_id],
            proposal=bw.current_proposal(ctx.key, group_id), inventory=ctx.inventory, privacy=ctx.privacy,
            file_ids=[c.file_id for c in chosen], visibility=pw.PRIVATE, ownership=args.ownership,
            coursework=args.coursework, license_id=args.license, attribution=args.attribution)
        print(f"{group_id}: staged as {stage.id}")
    return EXIT_OK


def _publish(args: argparse.Namespace, ctx: bw.Context, queue: bw.Queue, input_fn: Input) -> int:
    """One approval of an exact list. Anything unsafe is deferred, and the rest continue.

    The approval covers each stage's exact files, name and visibility, and is recorded for each stage,
    so every repository still goes through the same checks. A batch never creates a public repository.
    """
    _in_state(queue, args.groups, "needs_publish_approval")
    run = pcli.make_runner
    owner = pw.github_account(run)
    eligible, deferred = [], []
    for group_id in args.groups:
        stage = bw.latest_stage(ctx.key, group_id)
        group = ctx.run.group(group_id)
        inspector = bw.inspector_for(ctx, group)
        findings = pw.run_checks(stage, identity_terms=ctx.terms, inspector=inspector,
                                 committer_email=pw.committer_email(stage, run))
        if stage.visibility != pw.PRIVATE:
            deferred.append((group_id, "A batch only publishes private repositories."))
        elif pw.blockers(findings):
            deferred.append((group_id, "an unresolved high-risk finding"))
        elif not pw.name_is_free(owner, stage.repo_name, run):
            deferred.append((group_id, "a repository with that name already exists, and is never overwritten"))
        else:
            eligible.append((group_id, group, stage, inspector, findings))
    print(f"Scope of this approval: {len(eligible)} new PRIVATE repositories under {owner}. Nothing is made public.")
    for group_id, _, stage, _, findings in eligible:
        originals = sum(not f.generated for f in stage.files)
        accepted = sum(f.resolved is not None for f in findings)
        print(f"  {'[hidden]' if args.redact else stage.repo_name}  private, {originals} original file(s), {accepted} accepted "
              f"finding(s), ownership {stage.ownership}, stage {stage.id}")
    for group_id, reason in deferred:
        print(f"  deferred: {group_id}: {reason}")
    if not eligible:
        print("Nothing is eligible to publish.")
        return EXIT_OK
    word = f"publish {len(eligible)} private"
    if not _ask(input_fn, word):
        print("Not approved. Nothing was published.")
        return EXIT_ERROR
    append_audit(ctx.key, eligible[0][0], {"outcome": "private_scope_approved", "visibility": pw.PRIVATE,
                                           "stages": [[s.id, s.digest] for _, _, s, _, _ in eligible]})
    published, failed = [], []
    for group_id, group, stage, inspector, findings in eligible:  # one failure never stops the others
        try:
            pw.save_approval(pw.approve(stage, findings, owner, True), ctx.key, group_id)
            append_audit(ctx.key, group_id, {"stage_id": stage.id, "visibility": stage.visibility,
                                             "outcome": "publish_approved", "scope": "batch"})
            pcli._publish(Namespace(redact=args.redact), ctx.key, group, stage, ctx.terms, inspector)
            published.append(group_id)
        except pw.PublishRefused as exc:
            print(f"error: {group_id}: {exc}", file=sys.stderr)
            failed.append(group_id)
    print(f"\nPublished {len(published)} of {len(args.groups)} private repositories. Deferred: {len(deferred)}. "
          f"Failed: {len(failed)}. Making any of them public is a separate decision.")
    return EXIT_ERROR if failed else EXIT_OK
