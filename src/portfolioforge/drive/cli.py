"""Command line: ``python -m portfolioforge.drive``. Read-only discovery in Google Drive and Colab.

    connect [--client-file FILE]        allow listing your Drive's metadata; it cannot download anything
    connect --content                   also allow downloading the files you approve later (separate permission)
    status                              which permissions are held
    disconnect [--revoke]               delete the sign-ins here; --revoke also cancels them at Google
    scan [--folder NAME|ID ...] [--colab-only]
                                        write an inventory; scanning all of My Drive asks first
    duplicates                          compare the latest Drive inventory with a local one again

Then use the normal workflow: python -m portfolioforge.batch --inventory latest-drive run

Nothing here changes your Drive, downloads a file's contents, runs code, contacts an AI provider or publishes.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from typing import Callable, Sequence

from ..grouping.cli import UsageError, resolve_inventory
from ..grouping.config import load_grouping_config
from ..grouping.engine import propose_groups
from ..grouping.models import GroupingRun
from ..grouping.review import decisions_path, load_decisions
from ..grouping.store import load_run
from ..inventory import config as cfg
from ..inventory.cli import format_error, format_report
from ..inventory.config import ResourceLimits
from ..inventory.models import Inventory
from ..inventory.sinks import JsonlSink
from ..paths import inventories_dir
from ..privacy import PrivacyConfigError, load_privacy_settings
from . import auth, discovery, duplicates
from .auth import CONTENT_SCOPE, METADATA_SCOPE, DriveError
from .client import DriveClient

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INCOMPLETE = 3
Input = Callable[[str], str]


def make_client() -> DriveClient:
    """The discovery client, which holds only the metadata permission. Replaced in tests."""
    return DriveClient(auth.load_credentials(METADATA_SCOPE))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m portfolioforge.drive", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    connect = sub.add_parser("connect", help="sign in and grant one read-only permission")
    connect.add_argument("--client-file", metavar="FILE", help="the desktop-app OAuth client file from Google Cloud")
    connect.add_argument("--content", action="store_true", help="the separate permission to download files you approve")
    sub.add_parser("status", help="which permissions are held")
    sub.add_parser("disconnect", help="delete the sign-ins here").add_argument(
        "--revoke", action="store_true", help="also cancel them at Google")
    scan = sub.add_parser("scan", help="write an inventory of your Drive's metadata")
    scan.add_argument("--folder", action="append", default=[], metavar="NAME|ID",
                      help="a top-level folder's exact name, or any folder's ID (repeatable); default: all of My Drive")
    scan.add_argument("--colab-only", action="store_true", help="only notebooks, and the folders that lead to them")
    scan.add_argument("--include-shared", action="store_true", help="also include items you don't own")
    scan.add_argument("--include-images", action="store_true", help="also inventory image files")
    scan.add_argument("--keep-build-dirs", action="store_true", help="do not skip build/out/dist folders")
    scan.add_argument("--exclude", action="append", default=[], metavar="NAME", help="skip folders with this name (repeatable)")
    scan.add_argument("--max-files", type=int, default=cfg.DEFAULT_MAX_FILES, help="0 disables")
    scan.add_argument("--max-depth", type=int, default=cfg.DEFAULT_MAX_DEPTH, help="0 disables")
    scan.add_argument("--time-limit", type=float, default=cfg.DEFAULT_TIME_BUDGET_SECONDS / 60, metavar="MINUTES",
                      help="0 disables")
    scan.add_argument("--plan-only", action="store_true", help="show what would be scanned, and stop")
    scan.add_argument("--yes", action="store_true", help="skip the confirmation for scanning all of My Drive")
    scan.add_argument("--compare-with", default="latest", metavar="INVENTORY|none",
                      help="local inventory to check for duplicates (default: latest)")
    scan.add_argument("--redact", action="store_true", help="show no names or paths")
    scan.add_argument("--privacy-config", metavar="FILE", help="privacy settings file")
    again = sub.add_parser("duplicates", help="compare a Drive inventory with a local one again")
    again.add_argument("--inventory", default="latest-drive")
    again.add_argument("--compare-with", default="latest", metavar="INVENTORY")
    again.add_argument("--redact", action="store_true", help="show group IDs only")
    return parser


def main(argv: Sequence[str] | None = None, *, input_fn: Input = input) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return _run(args, input_fn)
    except (DriveError, UsageError) as exc:
        print(f"error: {exc}", file=sys.stderr)  # these messages never contain paths or tokens
        return EXIT_ERROR
    except (ValueError, OSError, PrivacyConfigError) as exc:
        print(format_error(exc, redact=getattr(args, "redact", False)), file=sys.stderr)
        return EXIT_ERROR


def _run(args: argparse.Namespace, input_fn: Input) -> int:
    if args.command == "connect":
        return _connect(args)
    if args.command == "status":
        for scope, name in ((METADATA_SCOPE, "List metadata"), (CONTENT_SCOPE, "Download approved files")):
            print(f"{name}: {'allowed' if auth.is_connected(scope) else 'not allowed'}")
        return EXIT_OK
    if args.command == "disconnect":
        print(f"Removed {auth.disconnect(revoke=args.revoke)} sign-in(s)"
              + (", and cancelled them at Google." if args.revoke else " from this computer."))
        return EXIT_OK
    if args.command == "scan":
        return _scan(args, input_fn)
    return _duplicates(args)


def _connect(args: argparse.Namespace) -> int:
    client = auth.save_client(args.client_file) if args.client_file else auth.load_client()
    scope = CONTENT_SCOPE if args.content else METADATA_SCOPE
    if args.content:
        print("This asks Google for permission to download any file in your Drive. PortfolioForge only downloads "
              "files you approve, one typed approval at a time, and can't change anything.")
    else:
        print("This asks Google for permission to list your Drive's metadata (names, sizes, dates). "
              "It can't download file contents or change anything.")
    auth.sign_in(scope, client)
    print("Connected. Your sign-in is stored only in your PortfolioForge settings folder.")
    return EXIT_OK


def _options(args: argparse.Namespace) -> discovery.DriveOptions:
    return discovery.DriveOptions(
        folders=tuple(args.folder), colab_only=args.colab_only, include_shared=args.include_shared,
        include_images=args.include_images, skip_build_dirs=not args.keep_build_dirs,
        exclude_names=frozenset(n.casefold() for n in args.exclude),
        private_names=load_privacy_settings(args.privacy_config).private_folder_names,
        limits=ResourceLimits(max_files=args.max_files or None, max_depth=args.max_depth or None,
                              time_budget_seconds=(args.time_limit * 60) or None),
    )


def _scan(args: argparse.Namespace, input_fn: Input) -> int:
    options = _options(args)
    client = make_client()
    targets = discovery.resolve_targets(client, options)
    started = datetime.now(UTC)
    key = discovery.new_inventory_key(started)
    print(f"Google Drive scan plan ({'notebooks only' if options.colab_only else 'all recognized project files'})")
    for index, target in enumerate(targets, start=1):
        print(f"  {'folder ' + str(index) if args.redact else target.root.label}")
    print("Reads metadata only: names, sizes, dates, checksums. No file's contents are downloaded. "
          "Results stay on this computer.")
    print("Skipped, as in a local scan: credential and private folders, sensitive names, shortcuts, environments, "
          "caches and build output." + ("" if options.include_shared else " Items you don't own are skipped too."))
    if args.plan_only:
        print("\nPlan only: nothing was scanned.")
        return EXIT_OK
    if not options.folders and not options.colab_only and not args.yes:
        try:
            answer = input_fn("\nThis lists all of My Drive. Type 'yes' to continue: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            print("Scan cancelled. Nothing was scanned.")
            return EXIT_ERROR

    listing = discovery.collect(client, options, targets)
    output_dir = inventories_dir() / key
    with JsonlSink(output_dir) as sink:
        sink.write_plan(discovery.make_plan(targets, output_dir))
        result = discovery.build_inventory_records(listing, options, key, sink, started)
        discovery.write_refs(output_dir, result.refs)
        sink.close(result.report)
    print(format_report(result.report, output_dir, redact=args.redact))
    if args.compare_with != "none":
        _compare(key, args.compare_with, redact=args.redact, missing_is_fine=args.compare_with == "latest")
    print(f"\nInventory: {key}. Next: python -m portfolioforge.batch --inventory {key} run")
    return EXIT_OK if result.report.complete else EXIT_INCOMPLETE


def _run_for(key: str, inventory: Inventory) -> GroupingRun:
    """The saved grouping if there is one. Otherwise worked out in memory, leaving that inventory's records alone."""
    return load_run(key) or propose_groups(inventory, load_grouping_config(None), load_decisions(decisions_path(key)),
                                           inventory_key=key)


def _compare(drive_ref: str, local_ref: str, *, redact: bool, missing_is_fine: bool) -> list[duplicates.CrossSourceMatch]:
    try:
        local_key, local = resolve_inventory(local_ref)
    except UsageError:
        if not missing_is_fine:
            raise
        print("\nNo local inventory to compare with, so duplicates weren't checked.")
        return []
    drive_key, drive = resolve_inventory(drive_ref)
    drive_run, local_run = _run_for(drive_key, drive), _run_for(local_key, local)
    matches = duplicates.find_matches(drive, drive_run, local, local_run, load_grouping_config(None))
    duplicates.save_matches(drive_key, local_key, matches)
    print(f"\nPossible duplicates with local inventory {local_key}: {len(matches)}. Nothing was hidden or removed.")
    titles = {g.id: g.user_title or g.working_title.text for run in (drive_run, local_run) for g in run.groups}
    for m in matches:
        names = f"{m.drive_group} / {m.local_group}" if redact else \
            f"{titles.get(m.drive_group)} [{m.drive_group}] / {titles.get(m.local_group)} [{m.local_group}]"
        print(f"  {m.kind}: {names}" + ("" if redact else f": {m.detail}"))
    return matches


def _duplicates(args: argparse.Namespace) -> int:
    _compare(args.inventory, args.compare_with, redact=args.redact, missing_is_fine=False)
    return EXIT_OK
