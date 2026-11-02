"""Command line: ``python -m portfolioforge.publishing``.

    candidates GROUP              files you may select, and what can never be selected
    stage GROUP PROPOSAL --file ID [--file ID ...] [--visibility {private,public}]
          --ownership O --course-policy P --license SPDX|none [--attribution TEXT]
    preview GROUP STAGE           exactly what would be published, and every finding
    resolve GROUP STAGE FINDING --reason TEXT   accept one high-risk finding, with a reason
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
    COURSE_POLICY,
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
    github_account,
    list_candidates,
    load_stage,
    name_is_free,
    publish,
    resolve_finding,
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
    stage.add_argument("--course-policy", choices=COURSE_POLICY)
    stage.add_argument("--license", metavar="SPDX|none")
    stage.add_argument("--attribution", help="credit for other people's code; goes in PROVENANCE.md")
    # Deliberately no --yes on approve: permission to publish is always typed by a person.
    for name in ("preview", "approve", "publish"):
        command = sub.add_parser(name)
        command.add_argument("group")
        command.add_argument("stage")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("group")
    resolve.add_argument("stage")
    resolve.add_argument("finding")
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
                             visibility=args.visibility, ownership=args.ownership, course_policy=args.course_policy,
                             license_id=args.license, attribution=args.attribution)
        findings = run_checks(stage, identity_terms=terms, inspector=inspector)
        print(f"Staged {len(stage.files)} file(s) as {stage.id}. Nothing was uploaded, and GitHub was not contacted.")
        print(f"  {len(blockers(findings))} blocking finding(s). Next: preview {group.id} {stage.id}")
        return EXIT_OK

    stage = load_stage(key, group.id, args.stage)
    email = committer_email(stage, make_runner)
    findings = run_checks(stage, identity_terms=terms, inspector=inspector, committer_email=email)

    if args.command == "resolve":
        match = next((f for f in findings if f.id == args.finding), None)
        if match is None:
            raise PublishRefused("No such finding for this stage.")
        answer = _ask(input_fn, f"Accept {match.check}/{match.kind} as safe to publish?\nType 'accept' to continue: ")
        if answer != "accept":
            print("Not accepted.")
            return EXIT_ERROR
        resolve_finding(stage, findings, args.finding, args.reason)
        append_audit(key, group.id, {"stage_id": stage.id, "outcome": "finding_accepted", "kind": match.kind})
        print("Accepted, with your reason recorded locally.")
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
