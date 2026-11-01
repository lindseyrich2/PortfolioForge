"""Command line: ``python -m portfolioforge.grouping``.

    propose                       group the inventory's files (metadata only)
    list [--flagged] [--band B] [--kind K]
    show GROUP                    evidence, files, flags and relationships
    accept | reject GROUP
    rename GROUP TITLE            your own working title (still not a final repo name)
    merge GROUP GROUP...          merge into the first group
    split GROUP FILE... [--title T]
    move FILE GROUP | unassign FILE
    label | unlabel GROUP {mine,third_party,ignored}
    confirm | dismiss RELATIONSHIP
    keep-both GROUP GROUP | mark-duplicate GROUP GROUP
    decisions                     your recorded decisions
    report                        write a local Markdown report

Works only on a saved inventory's metadata. Never opens original files.
Every correction is stored locally and re-applied on each run.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

from ..data import stored_inventories
from ..inventory.cli import format_error
from ..inventory.models import Inventory
from ..inventory.sinks import REPORT_NAME, load_inventory
from ..paths import inventories_dir
from .config import GroupingConfigError, load_grouping_config
from .engine import propose_groups
from .models import GroupingRun
from .report import format_group, format_list, markdown_report
from .review import DecisionError, append_decision, decisions_path, load_decisions
from .store import load_run, save_report, save_run

EXIT_OK = 0
EXIT_ERROR = 1

_DECISION_COMMANDS = {
    "accept": "accept", "reject": "reject", "rename": "rename", "merge": "merge", "split": "split",
    "move": "move", "unassign": "unassign", "label": "label", "unlabel": "unlabel",
    "confirm": "confirm", "dismiss": "dismiss", "keep-both": "keep_both", "mark-duplicate": "mark_duplicate",
}


class UsageError(ValueError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m portfolioforge.grouping",
        description="Propose and review candidate project groups from a saved inventory (metadata only).",
    )
    parser.add_argument("--inventory", default="latest",
                        help="inventory ID from 'python -m portfolioforge.data list', 'latest', or a folder")
    parser.add_argument("--config", metavar="FILE", help="grouping settings file (default: grouping.toml)")
    parser.add_argument("--redact", action="store_true", help="show no paths or file/folder names")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("propose")
    listing = sub.add_parser("list")
    listing.add_argument("--flagged", action="store_true")
    listing.add_argument("--band", choices=("high", "medium", "low"))
    listing.add_argument("--kind", choices=("course_collection", "project", "assignment", "loose_collection"))
    show = sub.add_parser("show")
    show.add_argument("group")
    for name in ("accept", "reject"):
        sub.add_parser(name).add_argument("group")
    rename = sub.add_parser("rename")
    rename.add_argument("group")
    rename.add_argument("title")
    sub.add_parser("merge").add_argument("groups", nargs="+")
    split = sub.add_parser("split")
    split.add_argument("group")
    split.add_argument("files", nargs="+")
    split.add_argument("--title")
    move = sub.add_parser("move")
    move.add_argument("file")
    move.add_argument("group")
    sub.add_parser("unassign").add_argument("file")
    for name in ("label", "unlabel"):
        p = sub.add_parser(name)
        p.add_argument("group")
        p.add_argument("label", choices=("mine", "third_party", "ignored"))
    for name in ("confirm", "dismiss"):
        sub.add_parser(name).add_argument("relationship")
    for name in ("keep-both", "mark-duplicate"):
        p = sub.add_parser(name)
        p.add_argument("first")
        p.add_argument("second")
    sub.add_parser("decisions")
    sub.add_parser("report")
    return parser


def resolve_inventory(ref: str) -> tuple[str, Inventory]:
    if ref == "latest":
        finished = [i for i in stored_inventories() if i.report is not None]
        if not finished:
            raise UsageError("No saved inventory found. Run an inventory scan first.")
        item = finished[-1]
        return item.id, load_inventory(item.path)
    candidate = inventories_dir() / ref
    if os.sep not in ref and "/" not in ref and (candidate / REPORT_NAME).is_file():
        return ref, load_inventory(candidate)
    folder = Path(ref)
    if (folder / REPORT_NAME).is_file():
        return folder.name, load_inventory(folder)
    raise UsageError("Inventory not found (use an ID from 'python -m portfolioforge.data list').")


def _propose(key: str, inventory: Inventory, config_path: str | None) -> GroupingRun:
    config = load_grouping_config(config_path)
    run = propose_groups(inventory, config, load_decisions(decisions_path(key)), inventory_key=key)
    save_run(run, key)
    return run


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except (ValueError, OSError) as exc:
        if isinstance(exc, (UsageError, DecisionError, GroupingConfigError)) and not args.redact:
            print(f"error: {exc}", file=sys.stderr)
        else:
            print(format_error(exc, redact=args.redact), file=sys.stderr)
        return EXIT_ERROR


def _run(args: argparse.Namespace) -> int:
    key, inventory = resolve_inventory(args.inventory)
    if args.command == "propose":
        run = _propose(key, inventory, args.config)
        print(format_list(run, redact=args.redact))
        return EXIT_OK

    run = load_run(key) or _propose(key, inventory, args.config)
    if args.command == "list":
        print(format_list(run, redact=args.redact, flagged_only=args.flagged, band=args.band, kind=args.kind))
        return EXIT_OK
    if args.command == "show":
        try:
            print(format_group(run, args.group, inventory, redact=args.redact))
        except KeyError:
            raise UsageError(f"No group {args.group!r} in the current proposal.") from None
        return EXIT_OK
    if args.command == "decisions":
        decisions = load_decisions(decisions_path(key))
        stale = set(run.stale_decisions)
        for d in decisions:
            mark = "  (out of date)" if d.id in stale else ""
            value = f" {d.value!r}" if d.value and not args.redact else ""
            print(f"{d.created_at:%Y-%m-%d %H:%M}  {d.action} {' '.join(d.targets)}{value}  [{d.id}]{mark}")
        if not decisions:
            print("No decisions recorded yet.")
        return EXIT_OK
    if args.command == "report":
        path = save_report(markdown_report(run, inventory), key)
        print(f"Report written to {'<data dir>/groupings/' + key + '/report.md' if args.redact else path}")
        print("It lists your file names and paths: keep it private.")
        return EXIT_OK

    action = _DECISION_COMMANDS[args.command]
    targets, value = _decision_arguments(args)
    _check_targets(run, inventory, action, targets)
    decision = append_decision(decisions_path(key), action, targets, value)
    run = _propose(key, inventory, args.config)
    status = "applied" if decision.id not in run.stale_decisions else "recorded, but its targets no longer exist"
    print(f"Decision {decision.id} {status}: {action} {' '.join(targets)}")
    return EXIT_OK


def _decision_arguments(args: argparse.Namespace) -> tuple[tuple[str, ...], str | None]:
    c = args.command
    if c in ("accept", "reject"):
        return (args.group,), None
    if c == "rename":
        return (args.group,), args.title
    if c == "merge":
        return tuple(args.groups), None
    if c == "split":
        return (args.group, *args.files), args.title
    if c == "move":
        return (args.file, args.group), None
    if c == "unassign":
        return (args.file,), None
    if c in ("label", "unlabel"):
        return (args.group,), args.label
    if c in ("confirm", "dismiss"):
        return (args.relationship,), None
    return (args.first, args.second), None


def _check_targets(run: GroupingRun, inventory: Inventory, action: str, targets: tuple[str, ...]) -> None:
    groups = {g.id for g in run.groups}
    files = {f.id for f in inventory.files}
    rels = {r.id for r in run.relationships}
    if action in ("confirm", "dismiss"):
        expected = [(targets[0], rels, "relationship")]
    elif action == "move":
        expected = [(targets[0], files, "file"), (targets[1], groups, "group")]
    elif action == "unassign":
        expected = [(targets[0], files, "file")]
    elif action == "split":
        expected = [(targets[0], groups, "group"), *((t, files, "file") for t in targets[1:])]
    else:
        expected = [(t, groups, "group") for t in targets]
    for target, known, kind in expected:
        if target not in known:
            raise UsageError(f"Unknown {kind} {target!r} (see 'list' and 'show').")
