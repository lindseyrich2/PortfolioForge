"""Command line: ``python -m portfolioforge.github``. Read-only discovery of GitHub repositories.

    scan [--repo OWNER/NAME ...] [--notebooks-only]   write an inventory; all reachable repositories asks first

Then review with: python -m portfolioforge.batch --inventory latest-github review

Uses your existing GitHub CLI sign-in. Nothing here fetches file contents, clones or changes a repository,
runs code, contacts an AI provider or publishes. Attribution labels are signals for review, not proof.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import UTC, datetime
from typing import Callable, Sequence

from ..drive.cli import _run_for
from ..drive.discovery import DriveOptions, build_inventory_records, make_plan
from ..drive.duplicates import find_matches, save_concerns, save_matches
from ..grouping.cli import UsageError, _propose, resolve_inventory
from ..grouping.config import load_grouping_config
from ..inventory import config as cfg
from ..inventory.cli import format_error, format_report
from ..inventory.config import ResourceLimits
from ..inventory.models import ScanMode
from ..inventory.sinks import JsonlSink
from ..paths import inventories_dir
from ..privacy import PrivacyConfigError, load_privacy_settings
from . import discovery
from .client import GitHubClient, GitHubError, is_full_name

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INCOMPLETE = 3
Input = Callable[[str], str]


def make_client() -> GitHubClient:
    """Replaced in tests."""
    return GitHubClient()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m portfolioforge.github", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan", help="write an inventory of your repositories' metadata")
    scan.add_argument("--repo", action="append", default=[], metavar="OWNER/NAME", help="only this repository (repeatable)")
    scan.add_argument("--notebooks-only", action="store_true", help="only notebooks, and the folders that lead to them")
    scan.add_argument("--include-images", action="store_true", help="also inventory image files")
    scan.add_argument("--keep-build-dirs", action="store_true", help="do not skip build/out/dist folders")
    scan.add_argument("--exclude", action="append", default=[], metavar="NAME", help="skip repositories and folders with this name")
    scan.add_argument("--max-commit-lookups", type=int, default=200, metavar="N",
                      help="check commit history for at most N notebooks (default: 200)")
    scan.add_argument("--compare-with", default="latest", metavar="INVENTORY|none", help="local inventory to check for duplicates")
    scan.add_argument("--compare-drive", default="latest-drive", metavar="INVENTORY|none", help="Drive inventory to check for duplicates")
    scan.add_argument("--read-local-remotes", action="store_true",
                      help="also read .git/config in the local inventory's clones, to match them by remote; reads nothing else")
    scan.add_argument("--yes", action="store_true", help="skip the confirmation for scanning every reachable repository")
    scan.add_argument("--redact", action="store_true", help="show no names or paths")
    scan.add_argument("--privacy-config", metavar="FILE", help="privacy settings file")
    return parser


def main(argv: Sequence[str] | None = None, *, input_fn: Input = input) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return _scan(args, input_fn)
    except (GitHubError, UsageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (ValueError, OSError, PrivacyConfigError) as exc:
        print(format_error(exc, redact=args.redact), file=sys.stderr)
        return EXIT_ERROR


def _chosen(client: GitHubClient, wanted: list[str]) -> list[dict]:
    if not wanted:
        return client.repositories()
    if not all(is_full_name(name) for name in wanted):
        raise UsageError("Give repositories as OWNER/NAME.")
    found = [client.repository(name) for name in dict.fromkeys(wanted)]
    if not all(found):
        raise UsageError("One of those repositories wasn't found, or you can't see it.")
    return found


def _scan(args: argparse.Namespace, input_fn: Input) -> int:
    client = make_client()
    login = client.login()
    repositories = _chosen(client, args.repo)
    print(f"GitHub scan plan: {len(repositories)} repositor{'y' if len(repositories) == 1 else 'ies'} you can reach.")
    print("Reads metadata only: names, sizes, hashes, ownership, forks and commit counts. No file's contents are fetched "
          "or copied. Results stay on this computer.")
    if not args.repo and not args.yes:
        try:
            answer = input_fn("\nThis lists every repository above, including private ones. Type 'yes' to continue: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            print("Scan cancelled. Nothing was scanned.")
            return EXIT_ERROR

    options = DriveOptions(
        colab_only=args.notebooks_only, include_shared=True, include_images=args.include_images, skip_build_dirs=not args.keep_build_dirs,
        exclude_names=frozenset(n.casefold() for n in args.exclude),
        private_names=load_privacy_settings(args.privacy_config).private_folder_names,
        limits=ResourceLimits(max_files=cfg.DEFAULT_MAX_FILES, max_depth=cfg.DEFAULT_MAX_DEPTH,
                              time_budget_seconds=cfg.DEFAULT_TIME_BUDGET_SECONDS),
    )
    found = discovery.collect(client, login, repositories, options, max_commit_lookups=args.max_commit_lookups)
    started = datetime.now(UTC)
    key = "github-" + started.strftime("%Y%m%dT%H%M%SZ")
    output_dir = inventories_dir() / key
    with JsonlSink(output_dir) as sink:
        sink.write_plan(make_plan(found.listing.targets, output_dir, mode=ScanMode.GITHUB))
        result = build_inventory_records(found.listing, options, key, sink, started, mode=ScanMode.GITHUB, locate=discovery.location)
        discovery.write_repos(output_dir, login, found.repos)
        sink.close(result.report)
    print(format_report(result.report, output_dir, redact=args.redact))

    inventory_key, inventory = resolve_inventory(key)
    run = _propose(inventory_key, inventory, None)
    save_concerns(key, "attribution", discovery.attribution_concerns(run, inventory, found.repos))
    _print_repositories(found, redact=args.redact)
    _compare(args, key, inventory, run, found, login)
    print(f"\nInventory: {key}. Next: python -m portfolioforge.batch --inventory {key} review")
    print("Nothing was fetched or copied. To analyze a project, clone it yourself and run the local scan.")
    return EXIT_OK if result.report.complete else EXIT_INCOMPLETE


def _print_repositories(found: discovery.GitHubListing, *, redact: bool) -> None:
    labels = Counter(r.label for r in found.repos)
    print("\nAttribution signals (for your review; " + discovery.NOT_PROOF + "):")
    print("  " + (", ".join(f"{label} {count}" for label, count in sorted(labels.items())) or "none"))
    if found.empty or found.left_out_private or found.left_out_self:
        print(f"  Left out: {found.empty} empty, {found.left_out_private} by your private or excluded names, "
              f"{found.left_out_self} PortfolioForge's own source")
    if redact:
        return
    for repo in found.repos:
        print(f"  {repo.label:<16} {repo.full_name}" + (" [private]" if repo.private else "") + f": {'; '.join(repo.reasons)}")


def _compare(args: argparse.Namespace, key: str, inventory, run, found: discovery.GitHubListing, login: str) -> None:
    config = load_grouping_config(None)
    for reference, other_label in ((args.compare_with, "local"), (args.compare_drive, "Google Drive")):
        if reference == "none":
            continue
        try:
            other_key, other = resolve_inventory(reference)
        except UsageError:
            print(f"\nNo {other_label} inventory to compare with, so duplicates there weren't checked.")
            continue
        other_run = _run_for(other_key, other)
        matches = find_matches(inventory, run, other, other_run, config)
        extra: dict[str, list[str]] = {}
        if other_label == "local" and args.read_local_remotes:
            clones, extra = discovery.local_clone_matches(other, other_run, inventory, run, found.repos, login)
            matches = _merged(matches, clones)
        save_matches(key, other_key, matches, cloud_label="GitHub", other_label=other_label)
        if extra:
            save_concerns(other_key, f"{key}-remotes", extra)
        titles = {g.id: g.user_title or g.working_title.text for r in (run, other_run) for g in r.groups}
        print(f"\nPossible duplicates with {other_label} inventory {other_key}: {len(matches)}. Nothing was hidden or removed.")
        for m in matches:
            names = f"{m.drive_group} / {m.local_group}" if args.redact else \
                f"{titles.get(m.drive_group)} [{m.drive_group}] / {titles.get(m.local_group, m.local_group)} [{m.local_group}]"
            print(f"  {m.kind}: {names}" + ("" if args.redact else f": {m.detail}"))


def _merged(matches, clones):
    """A remote is stronger evidence than a name, so it replaces a weaker match on the same pair."""
    pairs = {(m.drive_group, m.local_group): m for m in matches}
    pairs.update({(m.drive_group, m.local_group): m for m in clones})
    return sorted(pairs.values(), key=lambda m: (m.drive_group, m.local_group))
