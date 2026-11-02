"""Command line: ``python -m portfolioforge.publishing``.

    candidates GROUP              files you may select, and what can never be selected
    stage GROUP PROPOSAL --file ID [--file ID ...] [--visibility {private,public}]
          --ownership O --license SPDX|none [--coursework] [--attribution TEXT]
    refresh GROUP STAGE           regenerate README.md and PROVENANCE.md after new results; same files, same acceptances
    preview GROUP STAGE           exactly what would be published, and every finding
    review GROUP STAGE            unresolved findings by file and line, with their IDs
    open-finding GROUP STAGE FINDING   open that file at that line in VS Code; nothing is read or sent
    resolve GROUP STAGE FINDING [FINDING ...] --reason TEXT
                                  accept the named high-risk findings, with one reason
    approve GROUP STAGE           approve this exact stage (always asks you to type)
    publish GROUP STAGE           create the new repository with the GitHub CLI

Nothing is uploaded until an approved ``publish``. Visibility defaults to private.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import UTC, datetime
from typing import Callable, Sequence

from ..devtools.privacy_check import local_identity_terms
from ..grouping.cli import UsageError, resolve_inventory
from ..grouping.store import load_run
from ..inventory.cli import format_error
from ..privacy import PrivacyConfigError, load_privacy_settings
from ..understanding.build_files import project_facts
from ..understanding.eligibility import assess_run
from ..understanding.inspectors import DEFAULT_REGISTRY
from ..understanding.store import append_audit, load_proposal
from .workflow import (
    committer_email,
    OWNERSHIP,
    PRIVATE,
    PUBLIC,
    VISIBILITIES,
    PublishRefused,
    approve,
    blockers,
    create_stage,
    find_approval,
    format_preview,
    format_review,
    github_account,
    list_candidates,
    load_stage,
    name_is_free,
    open_in_editor,
    refresh_stage,
    publish,
    resolve_findings,
    run_checks,
    run_tool,
    save_approval,
)

EXIT_OK = 0
EXIT_ERROR = 1
#: Replaced in tests. The only way this package reaches git or the GitHub CLI.
make_runner = run_tool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m portfolioforge.publishing",
                                     description="Publish one accepted project to a new GitHub repository.")
    parser.add_argument("--inventory", default="latest")
    parser.add_argument("--redact", action="store_true", help="show no paths, names or README text")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("candidates").add_argument("group")
    stage = sub.add_parser("stage")
    stage.add_argument("group")
    stage.add_argument("proposal")
    stage.add_argument("--file", action="append", dest="files", default=[], metavar="FILE_ID",
                       help="a file to include; repeat for each. Nothing is included by default")
    stage.add_argument("--visibility", choices=VISIBILITIES, default=PRIVATE)
    stage.add_argument("--ownership", choices=OWNERSHIP)
    stage.add_argument("--coursework", action="store_true", help="note in PROVENANCE.md that this began as coursework")
    stage.add_argument("--license", metavar="SPDX|none")
    stage.add_argument("--attribution", help="credit for other people's code; goes in PROVENANCE.md")
    # Deliberately no --yes on approve: permission to publish is always typed by a person.
    for name in ("preview", "review", "refresh", "approve", "publish"):
        command = sub.add_parser(name)
        command.add_argument("group")
        command.add_argument("stage")
    open_finding = sub.add_parser("open-finding", help="open a finding's file at its line in VS Code")
    open_finding.add_argument("group")
    open_finding.add_argument("stage")
    open_finding.add_argument("finding")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("group")
    resolve.add_argument("stage")
    resolve.add_argument("finding", nargs="+", help="one or more finding IDs; nothing is selected for you")
    resolve.add_argument("--reason", required=True)
    return parser




def main(argv: Sequence[str] | None = None, *, input_fn: Callable[[str], str] = input) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args, input_fn)
    except (UsageError, PublishRefused) as exc:
        print(f"error: {exc}", file=sys.stderr)  # these messages never contain paths
        return EXIT_ERROR
    except (ValueError, OSError, PrivacyConfigError) as exc:
        print(format_error(exc, redact=args.redact), file=sys.stderr)
        return EXIT_ERROR


def _run(args: argparse.Namespace, input_fn: Callable[[str], str]) -> int:
    key, inventory = resolve_inventory(args.inventory)
    grouping = load_run(key)
    if grouping is None:
        raise UsageError("No grouping results yet. Run: python -m portfolioforge.grouping propose")
    group = grouping.group(args.group)
    if group is None:
        raise UsageError("No such group in the current grouping results.")
    privacy = load_privacy_settings()
    terms = local_identity_terms()
    inspector = DEFAULT_REGISTRY.select(project_facts(group, inventory))

    if args.command == "candidates":
        candidates, refused = list_candidates(group, inventory, privacy, key)
        print(f"Selectable files ({len(candidates)}). None are selected until you name them with --file:")
        for c in candidates:
            print(f"  {c.file_id}" + ("" if args.redact else f"  {c.project_path}  [{c.category}, {c.size_bytes:,} bytes]"))
        if refused:
            print(f"Can never be selected ({len(refused)}):")
            if not args.redact:
                print("\n".join(f"  {path}  ({reason})" for path, reason in refused))
        return EXIT_OK

    if args.command == "stage":
        eligibility = {e.group_id: e for e in assess_run(grouping, inventory)}[group.id]
        stage = create_stage(inventory_key=key, group=group, eligibility=eligibility, inventory=inventory,
                             proposal=load_proposal(key, group.id, args.proposal), privacy=privacy, file_ids=args.files,
                             visibility=args.visibility, ownership=args.ownership, coursework=args.coursework,
                             license_id=args.license, attribution=args.attribution)
        findings = run_checks(stage, identity_terms=terms, inspector=inspector)
        print(f"Staged {len(stage.files)} file(s) as {stage.id}. Nothing was uploaded, and GitHub was not contacted.")
        print(f"  {len(blockers(findings))} blocking finding(s). Next: preview {group.id} {stage.id}")
        return EXIT_OK

    stage = load_stage(key, group.id, args.stage)
    if args.command == "refresh":
        eligibility = {e.group_id: e for e in assess_run(grouping, inventory)}[group.id]
        refreshed, changed, dropped = refresh_stage(
            stage, group=group, eligibility=eligibility, inventory=inventory, privacy=privacy,
            proposal=load_proposal(key, group.id, stage.proposal_id), identity_terms=terms, inspector=inspector)
        print(f"New stage {refreshed.id} replaces {stage.id}. Nothing was uploaded.")
        print(f"  Files that differ: {('[hidden]' if args.redact else ', '.join(changed)) if changed else 'none'}")
        print(f"  Acceptances carried over: {len(refreshed.resolutions)}. Not carried over: {dropped}.")
        return EXIT_OK

    email = committer_email(stage, make_runner)
    findings = run_checks(stage, identity_terms=terms, inspector=inspector, committer_email=email)

    if args.command == "review":
        print(format_review(findings, redact_names=args.redact))
        return EXIT_OK

    if args.command == "open-finding":
        match = next((f for f in findings if f.id == args.finding), None)
        if match is None:
            raise PublishRefused("No such finding for this stage.")
        which = open_in_editor(stage, match, list_candidates(group, inventory, privacy, key)[0], make_runner)
        print(f"Opened {which} at line {match.line or 1} in VS Code ({match.kind}). "
              "Nothing was read into this output or sent anywhere.")
        return EXIT_OK

    if args.command == "resolve":
        by_id = {f.id: f for f in findings}
        selected = list(dict.fromkeys(args.finding))
        # Refused before asking, so you are never asked to confirm something that cannot be accepted.
        if not args.reason.strip():
            raise PublishRefused("Give a reason.")
        known = [by_id[i] for i in selected if i in by_id]
        if len(known) != len(selected):
            raise PublishRefused("A finding ID is not in this stage. Nothing was accepted.")
        pending = [f for f in known if f.resolved is None]
        print(f"Selected {len(pending)} finding(s) to accept, with this reason: {'[hidden]' if args.redact else args.reason.strip()}")
        for f in pending:
            where = "" if args.redact else f"  {f.path or 'project'}" + (f":{f.line}" if f.line else "")
            print(f"  {f.id}  {f.check}/{f.kind}{where}")
        if len(known) != len(pending):
            print(f"  {len(known) - len(pending)} already accepted: left as they are, with their original reasons.")
        fixed_only = sorted({f.kind for f in known if not f.resolvable})
        if fixed_only or not pending:
            resolve_findings(stage, findings, selected, args.reason)  # refuses, or changes nothing
            print("Nothing new to accept.")
            return EXIT_OK
        word = "accept" if len(pending) == 1 else f"accept {len(pending)}"
        if _ask(input_fn, f"Have you checked each one, and is it safe to publish?\nType '{word}' to continue: ") != word:
            print("Not accepted.")
            return EXIT_ERROR
        resolve_findings(stage, findings, selected, args.reason)
        for f in pending:
            append_audit(key, group.id, {"stage_id": stage.id, "finding_id": f.id, "outcome": "finding_accepted",
                                         "kind": f.kind})
        print(f"Accepted {len(pending)} finding(s), with your reason recorded locally.")
        return EXIT_OK

    if args.command == "publish":
        return _publish(args, key, group, stage, terms, inspector)

    if args.command == "approve" and args.redact:
        raise PublishRefused("Approval needs the full preview, so it can't be used with --redact.")
    runner = make_runner
    owner = github_account(runner)
    free = name_is_free(owner, stage.repo_name, runner)
    print(format_preview(stage, findings, owner=owner, name_free=free, committer=email, redact_names=args.redact))

    if args.command == "preview":
        print(f"\nNothing was uploaded. GitHub was only asked whether this name exists. To approve: approve {group.id} {stage.id}")
        return EXIT_OK

    # approve
    if blockers(findings):
        raise PublishRefused("Unresolved high-risk findings block publication.")
    word = f"publish public {stage.repo_name}" if stage.visibility == PUBLIC else f"publish {stage.repo_name}"
    warning = "\nTHIS WILL BE VISIBLE TO EVERYONE ON THE INTERNET." if stage.visibility == PUBLIC else ""
    answer = _ask(input_fn, f"\nApprove creating this new {stage.visibility} repository, once?{warning}\n"
                            f"Type '{word}' to continue: ")
    if answer != word:
        print("Not approved. Nothing was published.")
        return EXIT_ERROR
    approval = approve(stage, findings, owner, free)
    save_approval(approval, key, group.id)
    append_audit(key, group.id, {"stage_id": stage.id, "visibility": stage.visibility, "outcome": "publish_approved"})
    print(f"Approved for one publication. Nothing has been uploaded yet. Next: publish {group.id} {stage.id}")
    return EXIT_OK


def _ask(input_fn: Callable[[str], str], prompt: str) -> str:
    try:  # refused when nobody is there to type
        return input_fn(prompt).strip()
    except EOFError:
        return ""


def _publish(args: argparse.Namespace, key: str, group, stage, terms, inspector) -> int:
    approval = find_approval(stage)
    base = {"stage_id": stage.id, "visibility": stage.visibility}
    try:
        if approval is None:
            raise PublishRefused("This stage has no unused publication approval. Nothing was published.")
        # Spent before anything is uploaded, so a failure can never leave it reusable.
        spent = replace(approval, used_at=datetime.now(UTC).isoformat())
        save_approval(spent, key, group.id)
        url = publish(stage, approval, identity_terms=terms, inspector=inspector, run=make_runner)
    except PublishRefused as exc:
        append_audit(key, group.id, {**base, "outcome": "refused", "reason": str(exc)})
        raise
    append_audit(key, group.id, {**base, "outcome": "published"})
    print(f"Published as a new {stage.visibility} repository: {'[hidden]' if args.redact else url}")
    return EXIT_OK
