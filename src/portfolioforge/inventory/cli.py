"""Command line: ``python -m portfolioforge.inventory``.

Targeted scan:       python -m portfolioforge.inventory "C:\\Users\\me\\PycharmProjects" "D:\\School"
Full-computer plan:  python -m portfolioforge.inventory --full-computer --plan-only
Full-computer scan:  python -m portfolioforge.inventory --full-computer
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Sequence, TextIO

from . import config as cfg
from .config import FullScanOptions, InventoryConfig, ResourceLimits
from .models import ScanMode, ScanPlan, ScanReport
from .platform import background_priority, detect_system
from .roots import ProtectedLocationError, plan_full_computer, plan_targeted
from .scanner import ScanProgress, run_scan
from .sinks import JsonlSink, default_output_dir

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INCOMPLETE = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m portfolioforge.inventory",
        description=(
            "Read-only, metadata-only inventory of local academic and project files. "
            "Files are never opened, executed, modified or uploaded; results stay on this computer."
        ),
    )
    parser.add_argument("paths", nargs="*", help="folders to scan (targeted scan)")
    scope = parser.add_argument_group("full-computer scan (opt-in)")
    scope.add_argument("--full-computer", action="store_true",
                       help="scan your files across this computer; shows the plan and asks first")
    scope.add_argument("--include-drive", action="append", default=[], metavar="LETTER",
                       help="also scan an additional drive listed in the plan (repeatable)")
    scope.add_argument("--include-location", action="append", default=[], metavar="PATH",
                       help="also scan an optional location listed in the plan, such as an old profile")
    scope.add_argument("--plan-only", action="store_true", help="show the scan plan and exit")
    scope.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    options = parser.add_argument_group("scan settings")
    options.add_argument("--output-dir", metavar="PATH",
                         help=r"where to write results (default: %%LOCALAPPDATA%%\PortfolioForge\inventories\<time>)")
    options.add_argument("--include-images", action="store_true", help="also inventory image files")
    options.add_argument("--keep-build-dirs", action="store_true", help="do not skip build/out/dist folders")
    options.add_argument("--exclude", action="append", default=[], metavar="PATH",
                         help="skip this folder and everything below it (repeatable)")
    options.add_argument("--max-files", type=int, default=cfg.DEFAULT_MAX_FILES, help="0 disables")
    options.add_argument("--max-dirs", type=int, default=cfg.DEFAULT_MAX_DIRECTORIES, help="0 disables")
    options.add_argument("--max-depth", type=int, default=cfg.DEFAULT_MAX_DEPTH, help="0 disables")
    options.add_argument("--time-limit", type=float, default=cfg.DEFAULT_TIME_BUDGET_SECONDS / 60,
                         metavar="MINUTES", help="0 disables")
    options.add_argument("--normal-priority", action="store_true",
                         help="run at normal priority instead of background priority")
    options.add_argument("--quiet", action="store_true", help="no progress line")
    return parser


def main(argv: Sequence[str] | None = None, *, input_fn: Callable[[str], str] = input) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.full_computer and args.paths:
        parser.error("give folders to scan or --full-computer, not both")
    if not args.full_computer and not args.paths:
        parser.error("give one or more folders to scan, or use --full-computer")
    if (args.include_drive or args.include_location) and not args.full_computer:
        parser.error("--include-drive and --include-location need --full-computer")

    output_dir = Path(args.output_dir).resolve() if args.output_dir else default_output_dir()
    exclude_paths = tuple(str(Path(p).resolve()) for p in args.exclude)
    system = detect_system()
    try:
        if args.full_computer:
            plan = plan_full_computer(
                FullScanOptions(tuple(args.include_drive), tuple(args.include_location)),
                system=system,
                output_dir=output_dir,
                exclude_paths=exclude_paths,
            )
        else:
            plan = plan_targeted(
                args.paths, system=system, output_dir=output_dir, exclude_paths=exclude_paths
            )
    except (ProtectedLocationError, FileNotFoundError, NotADirectoryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    config = InventoryConfig(
        include_images=args.include_images,
        skip_build_dirs=not args.keep_build_dirs,
        exclude_paths=exclude_paths,
    )
    limits = ResourceLimits(
        max_files=args.max_files or None,
        max_directories=args.max_dirs or None,
        max_depth=args.max_depth or None,
        time_budget_seconds=(args.time_limit * 60) or None,
    )
    print(format_plan(plan, limits))

    if args.plan_only:
        print("\nPlan only: nothing was scanned.")
        return EXIT_OK
    if plan.mode is ScanMode.FULL_COMPUTER and not args.yes:
        try:
            answer = input_fn(
                "\nOnly file metadata is read: nothing is opened, changed or uploaded.\n"
                "Type 'yes' to start the full-computer scan: "
            )
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            print("Scan cancelled. Nothing was scanned.")
            return EXIT_ERROR

    progress = None if args.quiet else _ProgressLine(sys.stderr)
    with background_priority(enabled=not args.normal_priority), JsonlSink(output_dir) as sink:
        sink.write_plan(plan)
        report = run_scan(
            plan,
            sink=sink,
            config=config,
            limits=limits,
            allow_full_computer=plan.mode is ScanMode.FULL_COMPUTER,
            on_progress=progress,
        )
    print(format_report(report, output_dir))
    return EXIT_OK if report.complete else EXIT_INCOMPLETE


def format_plan(plan: ScanPlan, limits: ResourceLimits) -> str:
    title = "full computer" if plan.mode is ScanMode.FULL_COMPUTER else "targeted"
    lines = [f"Scan plan ({title})", "", f"Folders to scan ({len(plan.roots)}):"]
    for root in plan.roots:
        label = "" if root.label == root.path else f"  ({root.label})"
        lines.append(f"  [{root.origin.value}] {root.path}{label}")
    if plan.excluded:
        lines += ["", f"Excluded locations ({len(plan.excluded)}):"]
        lines += [f"  [{e.reason.value}] {e.path}" for e in plan.excluded]
    if plan.optional:
        lines += ["", "Optional locations, NOT included unless you opt in:"]
        for loc in plan.optional:
            hint = f"  -> {loc.include_hint}" if loc.include_hint else "  (cannot be included)"
            lines.append(f"  {loc.path}  {loc.description}{hint}")
    lines += [
        "",
        "Always skipped inside every folder: Windows/system and application folders, AppData,",
        "  credential folders (.ssh, .aws, ...), credential-looking files (names redacted), links",
        "  and junctions, cloud-only placeholders, virtual environments, installed R package",
        "  libraries, PortfolioForge's own source code, caches and build output.",
        "",
        "Limits: " + ", ".join([
            f"max files {_limit(limits.max_files)}",
            f"max folders {_limit(limits.max_directories)}",
            f"max depth {_limit(limits.max_depth)}",
            "time limit " + (
                "none" if limits.time_budget_seconds is None
                else f"{limits.time_budget_seconds / 60:g} min"
            ),
        ]),
        f"Results will be written to: {plan.output_dir}",
    ]
    return "\n".join(lines)


def format_report(report: ScanReport, output_dir: Path) -> str:
    s = report.summary
    lines = ["", "Scan finished." if report.complete else "Scan INCOMPLETE."]
    if report.stop_reason:
        lines.append(f"  Stopped early: {report.stop_reason}. Results cover only part of the plan.")
    if report.limits_reached:
        lines.append(f"  Some folders were skipped because of: {', '.join(report.limits_reached)}.")
    lines += [
        f"  Files inventoried:   {s.total_files:,} ({s.total_size_bytes / 1_048_576:,.1f} MB)",
        f"  Folders visited:     {s.directories_scanned:,}",
        f"  Entries skipped:     {s.skipped_entries:,}",
        f"  Errors:              {s.errors:,}",
    ]
    if s.files_by_category:
        lines.append("  By category:         " + ", ".join(f"{k} {v:,}" for k, v in s.files_by_category.items()))
    if s.skipped_by_reason:
        lines.append("  Skipped by reason:   " + ", ".join(f"{k} {v:,}" for k, v in s.skipped_by_reason.items()))
    if s.cloud_only_files_skipped:
        lines.append(f"  Cloud-only files not downloaded or inventoried: {s.cloud_only_files_skipped:,}")
    if s.excluded_image_files:
        lines.append(f"  Image files not inventoried (use --include-images): {s.excluded_image_files:,}")
    if s.unrecognized_files:
        lines.append(f"  Files with unrecognized extensions: {s.unrecognized_files:,}")
    lines.append(f"  Results: {output_dir}")
    return "\n".join(lines)


def _limit(value: int | None) -> str:
    return "none" if value is None else f"{value:,}"


class _ProgressLine:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def __call__(self, p: ScanProgress) -> None:
        minutes, seconds = divmod(int(p.elapsed_seconds), 60)
        text = (
            f"\r[folder {p.root_index}/{p.root_count}] {p.files_inventoried:,} files, "
            f"{p.directories_visited:,} folders, {p.skipped_entries:,} skipped, "
            f"{p.errors:,} errors, {minutes}m {seconds:02d}s"
        )
        self._stream.write(text.ljust(100) + ("\n" if p.finished else ""))
        self._stream.flush()
