"""Command line: ``python -m portfolioforge.understanding``.

    eligibility [--status S]                which groups look like technical projects (metadata only)
    select GROUP                            files that could be analyzed, and what is excluded
    approve-files GROUP [--file ID ...] [--acknowledge-review] [--yes]
                                            Gate 1: allow these files to be read on this computer
    extract GROUP                           read approved files locally; build a redacted evidence pack
    select-build-files GROUP                build configuration files that could be read (opens nothing)
    approve-build-files GROUP [--file ID ...] [--acknowledge-review] [--yes]
                                            separate approval to read those files on this computer
    inspect GROUP                           work out how to build and run; flag sensitive data (runs nothing)
    show-inspection GROUP                   the latest findings; flagged values are never shown
    plan-validation GROUP --action {compile,run} [--entry NAME] [--timeout S]
                                            show the exact commands a local validation would run
    approve-validation GROUP PLAN           approve that exact plan for one execution (always asks you to type)
    validate GROUP PLAN                     run an approved plan on a temporary copy
    apply-inspection GROUP PROPOSAL         add local findings to a proposal and its README (nothing is sent)
    manifest GROUP --provider {dry-run,anthropic} [--model M] [--with-inspection]
                                            show exactly what would be sent, to whom, and how much
    approve-manifest GROUP MANIFEST [--yes] Gate 2: approve that exact payload for that provider and model
    dry-run GROUP MANIFEST                  run a dry-run manifest through the send path (sends nothing)
    submit GROUP MANIFEST                   send an approved external manifest (asks you to type 'send')
    proposals GROUP | show-proposal GROUP PROPOSAL [--claims] | readme GROUP PROPOSAL
    edit-proposal GROUP PROPOSAL [--title] [--summary] [--name]
    accept-proposal GROUP PROPOSAL --name SLUG [--acknowledge-flags]
    status GROUP

There is no default provider. Nothing is ever published or created on GitHub.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from typing import Callable, Sequence

from ..devtools.privacy_check import local_identity_terms
from ..grouping.cli import UsageError, resolve_inventory
from ..grouping.models import GroupingRun
from ..grouping.store import load_run
from ..inventory.cli import format_error
from ..inventory.models import Inventory
from ..privacy import PrivacyConfigError, load_privacy_settings
from .analysis import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_EFFORT,
    DEFAULT_MAX_TOKENS,
    InvalidModelOutput,
    ProposalError,
    accept_proposal,
    build_proposal,
    edit_proposal,
)
from .anthropic_provider import ProviderRequestFailed
from .build_files import MAX_BUILD_FILE_BYTES, approve_build_files, propose_build_files
from .disclosure import (
    PROVIDERS,
    DisclosureNotApproved,
    ProviderNotConfigured,
    approve_manifest,
    build_manifest,
    format_manifest,
    get_provider,
    submit,
)
from .eligibility import assess_run
from .extract import build_evidence_pack, evidence_summary
from .findings import NOT_SENT_WITH_FINDINGS, apply_inspection, build_augmented_pack
from .inspection import inspect_project
from .models import BuildInspection, EligibilityStatus, ValidationResult
from .reader import ApprovedFileReader
from .selection import ApprovalError, approve_files, propose_selection
from .store import (
    accepted_names,
    append_audit,
    find_manifest_approval,
    find_validation_approval,
    latest_augmented_pack,
    latest_build_approval,
    latest_file_approval,
    latest_inspection,
    latest_manifest,
    latest_pack,
    list_proposals,
    list_validation_results,
    load_manifest,
    load_proposal,
    load_validation_plan,
    save_augmented_pack,
    save_build_approval,
    save_file_approval,
    save_inspection,
    save_manifest,
    save_manifest_approval,
    save_pack,
    save_proposal,
    save_readme,
    save_response,
    save_validation_approval,
    save_validation_plan,
    save_validation_result,
)
from .validation import (
    ACTIONS,
    ValidationRefused,
    ValidationUnavailable,
    approve_plan,
    build_plan,
    describe_outcome,
    execute_plan,
    format_plan,
    run_bounded,
)

EXIT_OK = 0
EXIT_ERROR = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m portfolioforge.understanding",
        description="Prepare approved project files for AI understanding. No AI provider is connected.",
    )
    parser.add_argument("--inventory", default="latest")
    parser.add_argument("--redact", action="store_true", help="show no paths or file/folder names")
    sub = parser.add_subparsers(dest="command", required=True)
    elig = sub.add_parser("eligibility")
    elig.add_argument("--status", choices=[s.value for s in EligibilityStatus])
    sub.add_parser("select").add_argument("group")
    approve = sub.add_parser("approve-files")
    approve.add_argument("group")
    approve.add_argument("--file", action="append", dest="files", metavar="FILE_ID",
                         help="approve only these files (default: all selectable files)")
    approve.add_argument("--acknowledge-review", action="store_true",
                         help="required for groups marked needs_review, after checking their reasons")
    approve.add_argument("--yes", action="store_true")
    sub.add_parser("extract").add_argument("group")
    manifest = sub.add_parser("manifest")
    manifest.add_argument("group")
    manifest.add_argument("--provider", required=True, choices=PROVIDERS,
                          help="dry-run (sends nothing) or anthropic (external, needs approval)")
    manifest.add_argument("--model", help=f"default: dry-run, or {DEFAULT_ANTHROPIC_MODEL} for anthropic")
    manifest.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    manifest.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"), default=DEFAULT_EFFORT)
    manifest.add_argument("--with-inspection", action="store_true",
                          help="add confirmed local findings; this makes a new payload needing a new approval")
    sub.add_parser("select-build-files", help="build configuration files that could be read").add_argument("group")
    approve_build = sub.add_parser("approve-build-files", help="separate approval to read build configuration files")
    approve_build.add_argument("group")
    approve_build.add_argument("--file", action="append", dest="files", metavar="FILE_ID",
                               help="approve only these files (default: all recognized build files)")
    approve_build.add_argument("--acknowledge-review", action="store_true")
    approve_build.add_argument("--yes", action="store_true")
    sub.add_parser("inspect", help="work out how to build and run; runs nothing").add_argument("group")
    sub.add_parser("show-inspection", help="the latest inspection findings").add_argument("group")
    plan = sub.add_parser("plan-validation", help="show the exact commands a validation would run; runs nothing")
    plan.add_argument("group")
    plan.add_argument("--action", required=True, choices=ACTIONS)
    plan.add_argument("--entry", metavar="NAME", help="entry point to run, when there is more than one")
    plan.add_argument("--timeout", type=int, metavar="SECONDS")
    # Deliberately no --yes: permission to run a project's code is always typed by a person.
    approve_v = sub.add_parser("approve-validation", help="approve one plan for one execution")
    approve_v.add_argument("group")
    approve_v.add_argument("plan")
    validate = sub.add_parser("validate", help="run an approved plan on a temporary copy")
    validate.add_argument("group")
    validate.add_argument("plan")
    apply_i = sub.add_parser("apply-inspection", help="add local findings to a proposal; nothing is sent")
    apply_i.add_argument("group")
    apply_i.add_argument("proposal")
    send = sub.add_parser("submit", help="send an approved manifest to its (external) provider")
    send.add_argument("group")
    send.add_argument("manifest")
    send.add_argument("--yes", action="store_true", help="skip the final 'send' confirmation")
    sub.add_parser("proposals").add_argument("group")
    show = sub.add_parser("show-proposal")
    show.add_argument("group")
    show.add_argument("proposal")
    show.add_argument("--claims", action="store_true", help="also show evidence references for each claim")
    readme = sub.add_parser("readme", help="write the draft README locally")
    readme.add_argument("group")
    readme.add_argument("proposal")
    readme.add_argument("--print", action="store_true", dest="print_readme")
    edit = sub.add_parser("edit-proposal")
    edit.add_argument("group")
    edit.add_argument("proposal")
    edit.add_argument("--title")
    edit.add_argument("--summary")
    edit.add_argument("--name", help="add your own repository name")
    accept = sub.add_parser("accept-proposal")
    accept.add_argument("group")
    accept.add_argument("proposal")
    accept.add_argument("--name", required=True, help="one of the valid suggested (or your own) names")
    accept.add_argument("--acknowledge-flags", action="store_true")
    approve_m = sub.add_parser("approve-manifest")
    approve_m.add_argument("group")
    approve_m.add_argument("manifest")
    approve_m.add_argument("--yes", action="store_true")
    dry = sub.add_parser("dry-run")
    dry.add_argument("group")
    dry.add_argument("manifest")
    sub.add_parser("status").add_argument("group")
    return parser


def main(argv: Sequence[str] | None = None, *, input_fn: Callable[[str], str] = input) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args, input_fn)
    except (UsageError, ApprovalError, DisclosureNotApproved, ProviderNotConfigured, ProposalError,
            ValidationRefused, ValidationUnavailable) as exc:
        print(f"error: {exc}", file=sys.stderr)  # these messages never contain paths
        return EXIT_ERROR
    except (ValueError, OSError, PrivacyConfigError) as exc:
        print(format_error(exc, redact=args.redact), file=sys.stderr)
        return EXIT_ERROR


def _context(args: argparse.Namespace) -> tuple[str, Inventory, GroupingRun]:
    key, inventory = resolve_inventory(args.inventory)
    run = load_run(key)
    if run is None:
        raise UsageError("No grouping results yet. Run: python -m portfolioforge.grouping propose")
    return key, inventory, run


def _group(run: GroupingRun, group_id: str):
    group = run.group(group_id)
    if group is None:
        raise UsageError(f"No group {group_id!r} in the current grouping results.")
    return group


def _title(group, redact: bool) -> str:
    return group.id if redact else f"{group.user_title or group.working_title.text} [{group.id}]"


def _confirm(prompt: str, word: str, args: argparse.Namespace, input_fn: Callable[[str], str]) -> bool:
    if getattr(args, "yes", False):
        return True
    try:
        return input_fn(f"{prompt}\nType '{word}' to continue: ").strip().lower() == word
    except EOFError:
        return False


#: Replaced in tests with a fake provider factory. Never contacts anything by itself.
make_provider = get_provider
#: Replaced in tests with a fake runner. Only reached through an approved validation plan.
make_runner = run_bounded


def _submit(args: argparse.Namespace, key: str, group, input_fn: Callable[[str], str]) -> int:
    manifest, payload = load_manifest(key, group.id, args.manifest)
    if manifest.provider == "dry-run":
        raise UsageError("This is a dry-run manifest; use the dry-run command.")
    approval = find_manifest_approval(manifest)
    if approval is None:
        append_audit(key, group.id, {"manifest_id": manifest.id, "provider": manifest.provider,
                                     "model": manifest.model, "outcome": "refused", "reason": "not approved"})
        raise DisclosureNotApproved("This disclosure manifest has not been approved.")
    cost = manifest.request.get("cost") or {}
    estimate = (f" Estimated cost: at most about ${cost['max_cost_usd']:.2f} (an upper bound, not an exact charge)."
                if "max_cost_usd" in cost else " Cost: unknown for this model; check current pricing.")
    prompt = (f"Send {manifest.payload_bytes:,} bytes to {manifest.provider} / {manifest.model} now? "
              f"The approved excerpts will leave this computer.{estimate}")
    if not args.yes:
        try:
            if input_fn(f"{prompt}\nType 'send' to continue: ").strip().lower() != "send":
                print("Not sent.")
                return EXIT_ERROR
        except EOFError:
            print("Not sent.")
            return EXIT_ERROR
    provider = make_provider(manifest.provider, manifest.model)
    base = {"manifest_id": manifest.id, "provider": manifest.provider, "model": manifest.model,
            "payload_bytes": manifest.payload_bytes, "payload_sha256": manifest.payload_sha256}
    try:
        result = submit(manifest, payload, approval, provider)
    except DisclosureNotApproved as exc:
        append_audit(key, group.id, {**base, "outcome": "refused", "reason": str(exc)})
        raise
    except ProviderRequestFailed as exc:
        append_audit(key, group.id, {**base, "outcome": "failed", "reason": str(exc)})
        raise UsageError(str(exc)) from None
    append_audit(key, group.id, {**base, "outcome": result.status, "sent": result.sent})
    if result.response is None:
        print(f"The provider returned no usable answer ({result.status}). Nothing was saved.")
        return EXIT_ERROR
    save_response(key, group.id, manifest.id, result.response)
    try:
        proposal = build_proposal(response_text=result.response, sent_body=json.loads(payload), inventory_key=key,
                                  group_id=group.id, manifest_id=manifest.id, provider=manifest.provider,
                                  model=manifest.model)
    except InvalidModelOutput as exc:
        append_audit(key, group.id, {**base, "outcome": "invalid_output", "reason": str(exc)})
        print(f"The answer did not match the required structure ({exc}). It was kept locally; nothing else saved.")
        return EXIT_ERROR
    save_proposal(proposal)
    claims = proposal.all_claims()
    verified = sum(c.status == "verified" for _, c in claims)
    print(f"Proposal {proposal.id}: {verified} of {len(claims)} claims verified, "
          f"{sum(n.valid for n in proposal.names)} valid name(s), {len(proposal.review_flags)} review flag(s).")
    print("Review it with: show-proposal " + group.id + " " + proposal.id)
    return EXIT_OK


def _format_inspection(i: BuildInspection, *, redact: bool) -> str:
    read = sum(f["status"] == "read" for f in i.files)
    supported = sum(c.status == "supported" for c in i.commands)
    guesses = len(i.commands) - supported
    high = sum(s.confidence == "high" for s in i.sensitive)
    lines = [f"Build inspection {i.id}",
             f"  Inspector: {i.inspector or 'none'}"]
    if i.manual_review_reason:
        lines.append(f"  MANUAL REVIEW NEEDED: {i.manual_review_reason}")
    lines += [
             f"  Files read: {read} of {len(i.files)} approved; {len(i.not_inspected)} project file(s) not read",
             f"  Build systems: {', '.join(i.build_systems) or 'none found'}",
             f"  Entry points: {len(i.entry_points)}; packages: {len(i.packages)}",
             f"  Version requirement stated: {'yes' if i.requirements else 'no'}",
             f"  Commands: {supported} supported by inspected files, {guesses} guesses (none have been run)",
             f"  Possible sensitive data: {len(i.sensitive)} finding(s), {high} likely (values are never shown)",
             f"  Risk signals: {len(i.risks)}"]
    if redact:
        return "\n".join(lines)
    not_read = [f for f in i.files if f["status"] != "read"]
    if not_read:
        lines.append("  Approved but not read:")
        lines += [f"    {f['project_path']}: {f['status']}" for f in not_read]
    if i.source_roots or i.test_roots:
        lines.append(f"  Source folders: {', '.join(i.source_roots) or 'none'}; "
                     f"test folders: {', '.join(i.test_roots) or 'none'}")
    if i.requirements:
        lines.append("  Version requirements:")
        lines += [f"    {r.setting}={r.value}  ({r.source.location})" for r in i.requirements]
    if i.packages:
        lines.append("  Packages:")
        lines += [f"    {p.name or '(default package)'}: {p.files} file(s)"
                  + ("" if p.location_matches else "  ⚠ not in the folder the package requires") for p in i.packages]
    if i.entry_points:
        lines.append("  Entry points:")
        for e in i.entry_points:
            where = e.source.location if e.source else "not seen in an inspected source"
            declared = f"; declared in {', '.join(r.location for r in e.declared_by)}" if e.declared_by else ""
            lines.append(f"    {e.name}  [{e.status}]  {where}{declared}" + (f"  ({e.note})" if e.note else ""))
    if i.dependencies:
        lines.append("  Declared dependencies: " + ", ".join(dict.fromkeys(d.detail for d in i.dependencies)))
    if i.commands:
        lines.append("  Commands (✓ supported by inspected files, ⚠ guess; none have been run):")
        for c in i.commands:
            mark = "✓" if c.status == "supported" else "⚠"
            folder = f" (in {c.working_dir})" if c.working_dir else ""
            lines.append(f"    {mark} [{c.purpose}] {c.command}{folder}  — {c.reason}")
    if i.sensitive:
        lines.append("  Review before publishing (values are not shown):")
        order = {"high": 0, "medium": 1, "low": 2}
        for s in sorted(i.sensitive, key=lambda s: (order.get(s.confidence, 3), s.project_path, s.line or 0)):
            where = f"{s.project_path}:{s.line}" if s.line else s.project_path
            lines.append(f"    [{s.confidence}] {where}  {s.kind}: {s.detail}")
    if i.risks:
        lines.append("  What this code does when run:")
        lines += [f"    {r.project_path}" + (f":{r.line}" if r.line else "") + f"  {r.detail}" for r in i.risks]
    if i.not_inspected:
        lines.append("  Not read, so not scanned:")
        lines += [f"    {path}" for path in i.not_inspected]
    if i.notes:
        lines.append("  Notes:")
        lines += [f"    - {note}" for note in i.notes]
    return "\n".join(lines)


def _format_validation(r: ValidationResult, *, redact: bool) -> str:
    lines = [f"Validation result {r.id}: {r.outcome}", f"  {describe_outcome(r)}"]
    if r.tool_versions:
        lines.append("  Tools: " + ", ".join(r.tool_versions.values()))
    for step in r.steps:
        status = "timed out" if step.timed_out else f"exit code {step.exit_code}"
        lines.append(f"  {step.label}: {status} ({step.duration_seconds}s)")
        if not redact:
            for label, text in (("output", step.stdout), ("errors", step.stderr)):
                if text.strip():
                    lines.append(f"    {label}:")
                    lines += [f"      {line}" for line in text.splitlines()[:40]]
    if not redact:
        lines += [f"  Note: {note}" for note in r.notes]
    lines.append("  Temporary copy deleted: " + ("yes" if r.copy_removed else "NO, delete it by hand"))
    return "\n".join(lines)


def _inspect(args: argparse.Namespace, key: str, group, inventory: Inventory, privacy) -> int:
    source_approval = latest_file_approval(key, group.id)
    build_approval = latest_build_approval(key, group.id)
    if source_approval is None and build_approval is None:
        raise UsageError("Nothing is approved for this group. Run approve-files and/or approve-build-files first.")
    inspection = inspect_project(
        inventory_key=key, group=group, inventory=inventory, proposal=propose_build_files(group, inventory, privacy),
        source_approval=source_approval,
        source_reader=ApprovedFileReader(source_approval, privacy) if source_approval else None,
        build_approval=build_approval,
        build_reader=ApprovedFileReader(build_approval, privacy, MAX_BUILD_FILE_BYTES) if build_approval else None,
        identity_terms=local_identity_terms(),
    )
    save_inspection(inspection)
    print(_format_inspection(inspection, redact=args.redact))
    print("\nNothing was run and nothing was sent anywhere.")
    return EXIT_OK


def _validate(args: argparse.Namespace, key: str, group, privacy) -> int:
    plan = load_validation_plan(key, group.id, args.plan)
    base = {"plan_id": plan.id, "action": plan.action, "plan_sha256": plan.plan_sha256}
    approval = find_validation_approval(plan)
    source_approval = latest_file_approval(key, group.id)
    try:
        if approval is None:
            raise ValidationRefused("This validation plan has no unused approval. Nothing was run.")
        if source_approval is None or source_approval.id != plan.source_approval_id:
            raise ValidationRefused("The file approval changed after this plan was made. Nothing was run.")
        # Spent before anything runs, so a crash can never leave it reusable.
        save_validation_approval(replace(approval, used_at=datetime.now(UTC)), plan)
        result = execute_plan(plan, approval, ApprovedFileReader(source_approval, privacy), runner=make_runner)
    except ValidationRefused as exc:
        append_audit(key, group.id, {**base, "outcome": "refused", "reason": str(exc)})
        raise
    save_validation_result(result)
    append_audit(key, group.id, {**base, "outcome": result.outcome,
                                 "exit_codes": [s.exit_code for s in result.steps], "copy_removed": result.copy_removed})
    print(_format_validation(result, redact=args.redact))
    print(f"\nTo use this in the README: apply-inspection {group.id} <PROPOSAL>")
    return EXIT_OK if result.outcome in ("compile_passed", "run_exit_zero") else EXIT_ERROR


def _format_proposal(p, key: str, group_id: str, *, redact: bool, claims: bool) -> str:
    claim_list = p.all_claims()
    lines = [f"Proposal {p.id} ({p.status}; ownership: {p.ownership})",
             f"  Provider / model: {p.provider} / {p.model}",
             f"  Claims: {sum(c.status == 'verified' for _, c in claim_list)} verified, "
             f"{sum(c.status == 'rejected' for _, c in claim_list)} rejected",
             f"  Names: {sum(n.valid for n in p.names)} valid of {len(p.names)}"]
    steps = p.usage.instructions()
    lines.append(f"  Setup/run/test steps: {sum(i.status == 'verified' for _, i in steps)} verified, "
                 f"{sum(i.status == 'unverified' for _, i in steps)} unverified, "
                 f"{sum(i.status == 'rejected' for _, i in steps)} rejected")
    if p.review_flags:
        lines.append("  Review flags:")
        lines += [f"    - {f}" for f in p.review_flags]
    if redact:
        return "\n".join(lines)
    pack = latest_pack(key, group_id)
    augmented = latest_augmented_pack(key, group_id)
    where = {i.id: i.location for source in (pack, augmented) if source for i in source.items}

    def show(label: str, c) -> None:
        mark = {"verified": "✓", "rejected": "✗", "your_wording": "✎"}.get(c.status, "?")
        lines.append(f"    {mark} [{label}] {c.text}" + (f"  — rejected: {c.reason}" if c.reason else ""))
        if claims:
            lines.extend(f"        evidence {e}: {where.get(e, '(not in the current evidence pack)')}"
                         for e in c.evidence_ids)

    lines.append(f"  Title: {p.user_title or p.title}" + ("  (your wording)" if p.user_title else ""))
    if p.user_summary:
        lines.append(f"  Summary (your wording): {p.user_summary}")
    lines.append("  Claims:")
    for label, c in claim_list:
        show(label, c)
    lines.append("  Repository names:")
    for n in p.names:
        lines.append(f"    {'✓' if n.valid else '✗'} {n.slug}  ({n.source})"
                     + (f"  — {'; '.join(n.problems)}" if n.problems else ""))
    if p.uncertainties:
        lines.append("  Uncertainties: " + "; ".join(p.uncertainties))
    if p.attribution_concerns:
        lines.append("  Attribution concerns: " + "; ".join(p.attribution_concerns))
    lines.append("  Setup / run / test:")
    for section, i in steps:
        mark = {"verified": "✓", "unverified": "⚠", "rejected": "✗"}[i.status]
        lines.append(f"    {mark} [{section}] {i.text}" + (f"  `{i.command}`" if i.command else "")
                     + (f"  — {i.reason}" if i.reason else ""))
    if p.local_findings:
        lines.append(f"  Local findings from inspection {p.local_findings.inspection_id}:")
        lines += [f"    {'✓' if c.status == 'supported' else '⚠'} [{c.purpose}] {c.command}"
                  for c in p.local_findings.commands]
        lines += [f"    validation: {v.outcome}" for v in p.local_findings.validations]
    return "\n".join(lines)


def _run(args: argparse.Namespace, input_fn: Callable[[str], str]) -> int:
    key, inventory, run = _context(args)
    privacy = load_privacy_settings()
    results = {e.group_id: e for e in assess_run(run, inventory)}

    if args.command == "eligibility":
        counts = Counter(e.status.value for e in results.values())
        print("Eligibility (metadata only; nothing has been approved or read):")
        for status in EligibilityStatus:
            print(f"  {status.value:<20} {counts.get(status.value, 0)}")
        print()
        for group in run.groups:
            e = results[group.id]
            if args.status and e.status.value != args.status:
                continue
            if e.status is EligibilityStatus.EXCLUDED and not args.status:
                continue
            reasons = "" if args.redact else f"  ({'; '.join(e.reasons)})"
            print(f"  {e.status.value:<20} {_title(group, args.redact)}  {group.kind.value}, "
                  f"{e.code_files + e.notebook_files} code/notebook of {e.total_files}{reasons}")
        return EXIT_OK

    group = _group(run, args.group)
    eligibility = results[group.id]

    if args.command == "select":
        proposal = propose_selection(group, inventory, privacy)
        print(f"{_title(group, args.redact)}: {eligibility.status.value}")
        if not args.redact:
            print("  Reasons: " + "; ".join(eligibility.reasons))
        print(f"  Selectable for local reading ({len(proposal.selected)}):")
        for s in proposal.selected:
            label = s.file_id if args.redact else f"{s.project_path}  {s.file_id}"
            print(f"    {label}  [{s.language}, {s.size_bytes:,} bytes]")
        reasons = Counter(x.reason for x in proposal.excluded)
        print(f"  Excluded ({len(proposal.excluded)}): " + (", ".join(f"{r} {n}" for r, n in reasons.items()) or "none"))
        if not args.redact:
            for x in proposal.excluded:
                print(f"    {x.project_path}  ({x.reason})")
        print("  Nothing has been read. Approve with: approve-files " + group.id)
        return EXIT_OK

    if args.command == "approve-files":
        proposal = propose_selection(group, inventory, privacy)
        record = approve_files(inventory_key=key, group=group, eligibility=eligibility, proposal=proposal,
                               inventory=inventory, file_ids=args.files, acknowledge_review=args.acknowledge_review)
        prompt = (f"PortfolioForge will read {len(record.files)} file(s) of {_title(group, args.redact)} on this "
                  "computer to extract evidence. Nothing is sent anywhere, and the files are not changed.")
        if not _confirm(prompt, "approve", args, input_fn):
            print("Not approved. Nothing was read.")
            return EXIT_ERROR
        save_file_approval(record)
        print(f"Approved {len(record.files)} file(s) for local reading: {record.id}")
        return EXIT_OK

    if args.command == "select-build-files":
        proposal = propose_build_files(group, inventory, privacy)
        print(f"{_title(group, args.redact)}: build configuration files")
        for note in proposal.notes:
            print(f"  {note}")
        print(f"  Could be read ({len(proposal.candidates)}):")
        for c in proposal.candidates:
            label = c.file_id if args.redact else f"{c.project_path}  {c.file_id}"
            print(f"    {label}  [{c.role}, {c.size_bytes:,} bytes]")
        if proposal.blocked:
            print(f"  May not be read ({len(proposal.blocked)}):")
            for b in proposal.blocked:
                print(f"    {'[hidden]' if args.redact else b.project_path}  ({b.reason})")
        if proposal.review_only:
            print(f"  Never read, because they often hold credentials ({len(proposal.review_only)}); review by hand:")
            if not args.redact:
                print("\n".join(f"    {path}" for path in proposal.review_only))
        print(f"  Nothing has been read. Approve with: approve-build-files {group.id}")
        return EXIT_OK

    if args.command == "approve-build-files":
        proposal = propose_build_files(group, inventory, privacy)
        record = approve_build_files(inventory_key=key, group=group, eligibility=eligibility, proposal=proposal,
                                     file_ids=args.files, acknowledge_review=args.acknowledge_review)
        prompt = (f"PortfolioForge will read {len(record.files)} build configuration file(s) of "
                  f"{_title(group, args.redact)} on this computer. Nothing is run or sent, and the files are not changed.")
        if not _confirm(prompt, "approve", args, input_fn):
            print("Not approved. Nothing was read.")
            return EXIT_ERROR
        save_build_approval(record)
        print(f"Approved {len(record.files)} build file(s) for local reading: {record.id}")
        return EXIT_OK

    if args.command == "inspect":
        return _inspect(args, key, group, inventory, privacy)

    if args.command == "show-inspection":
        inspection = latest_inspection(key, group.id)
        if inspection is None:
            raise UsageError("No inspection for this group. Run inspect first.")
        print(_format_inspection(inspection, redact=args.redact))
        return EXIT_OK

    if args.command == "plan-validation":
        inspection = latest_inspection(key, group.id)
        approval = latest_file_approval(key, group.id)
        if inspection is None or approval is None:
            raise UsageError("Run approve-files and inspect first.")
        plan = build_plan(inspection=inspection, source_approval=approval, reader=ApprovedFileReader(approval, privacy),
                          action=args.action, entry_point=args.entry, timeout_seconds=args.timeout)
        save_validation_plan(plan)
        print(format_plan(plan, redact_names=args.redact))
        print(f"\nNothing was run. To approve this exact plan: approve-validation {group.id} {plan.id}")
        return EXIT_OK

    if args.command == "approve-validation":
        plan = load_validation_plan(key, group.id, args.plan)
        print(format_plan(plan, redact_names=args.redact))
        word = "run my code" if plan.runs_project_code else plan.action
        try:  # never skippable, and refused when nobody is there to type
            answer = input_fn(f"\nApprove this exact plan for one execution?\nType '{word}' to continue: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != word:
            print("Not approved. Nothing was run.")
            return EXIT_ERROR
        approval = approve_plan(plan)
        save_validation_approval(approval, plan)
        append_audit(key, group.id, {"plan_id": plan.id, "action": plan.action, "plan_sha256": plan.plan_sha256,
                                     "outcome": "validation_approved"})
        print(f"Approved {approval.id} for one {plan.action}. Nothing has run yet. Next: validate {group.id} {plan.id}")
        return EXIT_OK

    if args.command == "validate":
        return _validate(args, key, group, privacy)

    if args.command == "apply-inspection":
        proposal = load_proposal(key, group.id, args.proposal)
        inspection = latest_inspection(key, group.id)
        if inspection is None:
            raise UsageError("No inspection for this group. Run inspect first.")
        updated = apply_inspection(proposal, inspection, tuple(list_validation_results(key, group.id)))
        save_proposal(updated)
        print(f"Proposal {updated.id} updated from inspection {inspection.id}. Nothing was sent anywhere.")
        print(f"  Review flags now: {len(updated.review_flags)}")
        if not args.redact:
            print("\n".join(f"    - {flag}" for flag in updated.review_flags))
        print(f"  Regenerate the draft with: readme {group.id} {updated.id}")
        return EXIT_OK

    if args.command == "extract":
        approval = latest_file_approval(key, group.id)
        if approval is None:
            raise UsageError("No approved files for this group. Run approve-files first.")
        reader = ApprovedFileReader(approval, privacy)
        pack = build_evidence_pack(approval, reader)
        save_pack(pack)
        read = sum(1 for f in pack.files if f["status"] == "read")
        print(f"Evidence pack {pack.id}: {read} of {len(pack.files)} approved file(s) read, {len(pack.items)} excerpts")
        print("  By kind: " + (", ".join(f"{k} {v}" for k, v in evidence_summary(pack).items()) or "none"))
        print("  Technologies detected locally: " + (", ".join(t.name for t in pack.technologies) or "none"))
        not_read = [f for f in pack.files if f["status"] != "read"]
        if not_read:
            print(f"  Not read: {len(not_read)}" + ("" if args.redact else
                  " (" + "; ".join(f"{f['project_path']}: {f['status']}" for f in not_read) + ")"))
        if pack.warnings and not args.redact:
            print("  Notes: " + "; ".join(pack.warnings))
        print("  Nothing was sent anywhere. Next: manifest " + group.id + " --provider dry-run")
        return EXIT_OK

    if args.command == "manifest":
        model = args.model or (DEFAULT_ANTHROPIC_MODEL if args.provider == "anthropic" else "dry-run")
        get_provider(args.provider, model)  # fails unless the provider exists; contacts nothing
        pack = latest_pack(key, group.id)
        if pack is None:
            raise UsageError("No evidence pack for this group. Run extract first.")
        also_not_sent: tuple[str, ...] = ()
        inspection = None
        if args.with_inspection:
            inspection = latest_inspection(key, group.id)
            if inspection is None:
                raise UsageError("No inspection for this group. Run inspect first.")
            if inspection.manual_review_reason:
                raise UsageError(f"Manual review needed, so there are no findings to add. "
                                 f"{inspection.manual_review_reason}")
            pack = build_augmented_pack(pack, inspection)
            save_augmented_pack(pack)
            also_not_sent = NOT_SENT_WITH_FINDINGS
        manifest, payload = build_manifest(pack, args.provider, model, max_tokens=args.max_tokens,
                                           effort=args.effort, also_not_sent=also_not_sent)
        save_manifest(manifest, payload)
        print(format_manifest(manifest, redact=args.redact))
        if inspection is not None:
            print("\nThis manifest adds confirmed local findings, so it is a new payload: earlier approvals do "
                  "not cover it.")
            high = sum(s.confidence == "high" for s in inspection.sensitive)
            if high:
                print(f"The inspection has {high} likely credential or identifier finding(s). Excerpts are redacted, "
                      "but redaction can miss things: check show-inspection before approving.")
        print(f"\nNothing has been sent. To approve: approve-manifest {group.id} {manifest.id}")
        return EXIT_OK

    if args.command == "approve-manifest":
        manifest, _ = load_manifest(key, group.id, args.manifest)
        print(format_manifest(manifest, redact=args.redact))
        external = manifest.provider != "dry-run"
        prompt = (f"\nApprove sending exactly this payload ({manifest.payload_bytes:,} bytes, SHA-256 "
                  f"{manifest.payload_sha256[:12]}…) to {manifest.provider} / {manifest.model}?"
                  + ("\nThis provider is EXTERNAL: on submit, these excerpts leave your computer and the "
                     "provider's data-retention policy applies." if external else ""))
        if not _confirm(prompt, "approve", args, input_fn):
            print("Not approved.")
            return EXIT_ERROR
        approval = approve_manifest(manifest)
        save_manifest_approval(approval, manifest)
        print(f"Disclosure approved: {approval.id} (valid only for this exact payload, provider and model)")
        return EXIT_OK

    if args.command == "dry-run":
        manifest, payload = load_manifest(key, group.id, args.manifest)
        if manifest.provider != "dry-run":
            raise UsageError("This manifest is for an external provider. The dry-run command only runs dry-run "
                             "manifests; use 'submit' to send it after approval.")
        provider = get_provider(manifest.provider, manifest.model)
        approval = find_manifest_approval(manifest)
        try:
            result = submit(manifest, payload, approval, provider)
        except DisclosureNotApproved as exc:
            append_audit(key, group.id, {"manifest_id": manifest.id, "provider": manifest.provider,
                                         "model": manifest.model, "outcome": "refused", "reason": str(exc)})
            raise
        append_audit(key, group.id, {"manifest_id": manifest.id, "provider": result.provider, "model": result.model,
                                     "outcome": result.status, "sent": result.sent,
                                     "payload_bytes": result.payload_bytes, "payload_sha256": result.payload_sha256})
        print(f"Dry run complete: {result.payload_bytes:,} bytes checked against the approved manifest.")
        print(f"  Sent to an AI provider: {'yes' if result.sent else 'no'} ({result.status})")
        return EXIT_OK

    if args.command == "submit":
        return _submit(args, key, group, input_fn)
    if args.command == "proposals":
        proposals = list_proposals(key, group.id)
        if not proposals:
            print("No proposals yet.")
        for p in proposals:
            valid = sum(n.valid for n in p.names)
            print(f"  {p.id}  {p.status}  {p.provider}/{p.model}  {valid} valid name(s), "
                  f"{len(p.review_flags)} review flag(s)" + (f", accepted name chosen" if p.accepted_name else ""))
        return EXIT_OK
    if args.command == "show-proposal":
        proposal = load_proposal(key, group.id, args.proposal)
        print(_format_proposal(proposal, key, group.id, redact=args.redact, claims=args.claims))
        return EXIT_OK
    if args.command == "readme":
        proposal = load_proposal(key, group.id, args.proposal)
        path = save_readme(proposal)
        if args.print_readme and not args.redact:
            print(proposal.readme_markdown())
        print(f"Draft README written locally: {'<data dir>/understanding/…/' + path.name if args.redact else path}")
        print("Nothing was published. Review it before using it anywhere.")
        return EXIT_OK
    if args.command == "edit-proposal":
        proposal = load_proposal(key, group.id, args.proposal)
        edited = edit_proposal(proposal, title=args.title, summary=args.summary, add_name=args.name)
        save_proposal(edited)
        print(f"Proposal {edited.id} updated (your wording is labelled as yours, not as verified).")
        return EXIT_OK
    if args.command == "accept-proposal":
        proposal = load_proposal(key, group.id, args.proposal)
        accepted = accept_proposal(proposal, args.name, acknowledge_flags=args.acknowledge_flags,
                                   taken_names=accepted_names(key, except_group=group.id))
        save_proposal(accepted)
        print(f"Accepted name and description for {group.id}. Nothing was created or published on GitHub.")
        return EXIT_OK

    # status
    approval = latest_file_approval(key, group.id)
    pack = latest_pack(key, group.id)
    manifest = latest_manifest(key, group.id)
    print(f"{_title(group, args.redact)}")
    print(f"  Eligibility:      {eligibility.status.value}")
    print(f"  Group status:     {group.status.value}")
    print(f"  Files approved:   {len(approval.files) if approval else 0}")
    print(f"  Evidence pack:    {pack.id + f' ({len(pack.items)} excerpts)' if pack else 'none'}")
    print(f"  Latest manifest:  {manifest.id if manifest else 'none'}"
          + (f" ({'approved' if find_manifest_approval(manifest) else 'not approved'})" if manifest else ""))
    build_approval = latest_build_approval(key, group.id)
    inspection = latest_inspection(key, group.id)
    results = list_validation_results(key, group.id)
    print(f"  Build files approved: {len(build_approval.files) if build_approval else 0}")
    found = f"{inspection.id} ({len(inspection.sensitive)} possible sensitive finding(s))" if inspection else "none"
    print(f"  Inspection:       {found}")
    print(f"  Latest validation: {results[-1].outcome if results else 'none'}")
    return EXIT_OK
