"""Command line: ``python -m portfolioforge.understanding``.

    eligibility [--status S]                which groups look like technical projects (metadata only)
    select GROUP                            files that could be analyzed, and what is excluded
    approve-files GROUP [--file ID ...] [--acknowledge-review] [--yes]
                                            Gate 1: allow these files to be read on this computer
    extract GROUP                           read approved files locally; build a redacted evidence pack
    manifest GROUP --provider dry-run       show exactly what would be sent, to whom, and how much
    approve-manifest GROUP MANIFEST [--yes] Gate 2: approve that exact payload for that provider
    dry-run GROUP MANIFEST                  run the send path with the dry-run provider (sends nothing)
    status GROUP

No AI provider is connected in this version, and there is no default provider.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from typing import Callable, Sequence

from ..grouping.cli import UsageError, resolve_inventory
from ..grouping.models import GroupingRun
from ..grouping.store import load_run
from ..inventory.cli import format_error
from ..inventory.models import Inventory
from ..privacy import PrivacyConfigError, load_privacy_settings
from .disclosure import (
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
from .models import EligibilityStatus
from .reader import ApprovedFileReader
from .selection import ApprovalError, approve_files, propose_selection
from .store import (
    append_audit,
    find_manifest_approval,
    latest_file_approval,
    latest_manifest,
    latest_pack,
    load_manifest,
    save_file_approval,
    save_manifest,
    save_manifest_approval,
    save_pack,
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
    manifest.add_argument("--provider", required=True, help="only 'dry-run' is available")
    manifest.add_argument("--model", default="dry-run")
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
    except (UsageError, ApprovalError, DisclosureNotApproved, ProviderNotConfigured) as exc:
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
        get_provider(args.provider, args.model)  # fails unless the provider exists
        pack = latest_pack(key, group.id)
        if pack is None:
            raise UsageError("No evidence pack for this group. Run extract first.")
        manifest, payload = build_manifest(pack, args.provider, args.model)
        save_manifest(manifest, payload)
        print(format_manifest(manifest, redact=args.redact))
        print(f"\nNothing has been sent. To approve: approve-manifest {group.id} {manifest.id}")
        return EXIT_OK

    if args.command == "approve-manifest":
        manifest, _ = load_manifest(key, group.id, args.manifest)
        print(format_manifest(manifest, redact=args.redact))
        prompt = (f"\nApprove sending exactly this payload ({manifest.payload_bytes:,} bytes, SHA-256 "
                  f"{manifest.payload_sha256[:12]}…) to {manifest.provider} / {manifest.model}?")
        if not _confirm(prompt, "approve", args, input_fn):
            print("Not approved.")
            return EXIT_ERROR
        approval = approve_manifest(manifest)
        save_manifest_approval(approval, manifest)
        print(f"Disclosure approved: {approval.id} (valid only for this exact payload, provider and model)")
        return EXIT_OK

    if args.command == "dry-run":
        manifest, payload = load_manifest(key, group.id, args.manifest)
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
    return EXIT_OK
