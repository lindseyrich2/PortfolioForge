"""Command line: ``python -m portfolioforge.inventory``.

Targeted scan:       python -m portfolioforge.inventory "C:\\Users\\me\\PycharmProjects" "D:\\School"
Full-computer plan:  python -m portfolioforge.inventory --full-computer --plan-only
Full-computer scan:  python -m portfolioforge.inventory --full-computer
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Callable, Sequence, TextIO

from ..paths import UnsafeOutputLocation, ensure_safe_output_dir, inventories_dir
from ..privacy import PrivacyConfigError, load_privacy_settings
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
                         help="where to write results (default: <data dir>/inventories/<time>; "
                              "see python -m portfolioforge.data where). Never inside a Git repository")
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
    options.add_argument("--redact", action="store_true",
                         help="show no paths or file/folder names in the plan, summary or error "
                              "messages (safe to share)")
    options.add_argument("--privacy-config", metavar="FILE",
                         help="privacy settings file (default: privacy.toml in your settings folder)")
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
    try:
        return _run(args, input_fn)
    except (ValueError, OSError) as exc:
        print(format_error(exc, redact=args.redact), file=sys.stderr)
        return EXIT_ERROR


def format_error(exc: BaseException, *, redact: bool = False) -> str:
    """User-facing error text. With ``redact``, never includes paths or file names.

    Exception messages are where paths and names appear, so in redacted mode
    only a fixed description of the error type is shown.
    """
    if not redact:
        return f"error: {exc}"
    return f"error: {_redacted_description(exc)} (details hidden by --redact)"


def _redacted_description(exc: BaseException) -> str:
    if isinstance(exc, UnsafeOutputLocation):
        return "refusing to write PortfolioForge data inside a Git repository"
    if isinstance(exc, ProtectedLocationError):
        return "refusing to scan a protected, private or linked location"
    if isinstance(exc, PrivacyConfigError):
        return "the privacy settings file is missing or invalid"
    if isinstance(exc, FileExistsError):
        return "the output folder already contains an inventory"
    if isinstance(exc, NotADirectoryError):
        return "a given path is not a folder"
    if isinstance(exc, FileNotFoundError):
        return "a folder was not found"
    if isinstance(exc, PermissionError):
        return "permission denied"
    if isinstance(exc, OSError):
        return f"file system error ({type(exc).__name__})"
    if isinstance(exc, ValueError):
        return "invalid scan options"
    return type(exc).__name__


def _run(args: argparse.Namespace, input_fn: Callable[[str], str]) -> int:
    output_dir = Path(os.path.abspath(args.output_dir)) if args.output_dir else default_output_dir()
    exclude_paths = tuple(os.path.abspath(p) for p in args.exclude)
    ensure_safe_output_dir(output_dir)
    privacy = load_privacy_settings(args.privacy_config)

    system = detect_system()
    private = {
        "private_paths": privacy.private_paths,
        "private_folder_names": privacy.private_folder_names,
    }
    if args.full_computer:
        plan = plan_full_computer(
            FullScanOptions(tuple(args.include_drive), tuple(args.include_location)),
            system=system,
            output_dir=output_dir,
            exclude_paths=exclude_paths,
            **private,
        )
    else:
        plan = plan_targeted(
            args.paths, system=system, output_dir=output_dir, exclude_paths=exclude_paths, **private
        )

    config = InventoryConfig(
        include_images=args.include_images,
        skip_build_dirs=not args.keep_build_dirs,
        exclude_paths=exclude_paths,
    ).with_privacy(privacy)
    limits = ResourceLimits(
        max_files=args.max_files or None,
        max_directories=args.max_dirs or None,
        max_depth=args.max_depth or None,
        time_budget_seconds=(args.time_limit * 60) or None,
    )
    print(format_plan(plan, limits, redact=args.redact))

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
    print(format_report(report, output_dir, redact=args.redact))
    return EXIT_OK if report.complete else EXIT_INCOMPLETE


def format_plan(plan: ScanPlan, limits: ResourceLimits, *, redact: bool = False) -> str:
    """Human-readable plan. With ``redact``, no paths or folder names are shown."""
    title = "full computer" if plan.mode is ScanMode.FULL_COMPUTER else "targeted"
    lines = [f"Scan plan ({title})", "", f"Folders to scan ({len(plan.roots)}):"]
    for index, root in enumerate(plan.roots, start=1):
        if redact:
            lines.append(f"  [{root.origin.value}] root {index}")
        else:
            label = "" if root.label == root.path else f"  ({root.label})"
            lines.append(f"  [{root.origin.value}] {root.path}{label}")
    if plan.excluded:
        lines += ["", f"Excluded locations ({len(plan.excluded)}):"]
        if redact:
            counts = Counter(e.reason.value for e in plan.excluded)
            lines += [f"  [{reason}] {count}" for reason, count in sorted(counts.items())]
        else:
            lines += [f"  [{e.reason.value}] {e.path}" for e in plan.excluded]
    if plan.optional:
        lines += ["", "Optional locations, NOT included unless you opt in:"]
        if redact:
            counts = Counter(loc.kind for loc in plan.optional)
            lines += [f"  {kind}: {count}" for kind, count in sorted(counts.items())]
        else:
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
        f"Results will be written to: {_show_output(plan.output_dir, redact)}",
    ]
    return "\n".join(lines)


def _show_output(output_dir: str | Path | None, redact: bool) -> str:
    if output_dir is None:
        return "(not set)"
    if not redact:
        return str(output_dir)
    folder = Path(output_dir)
    if os.path.normcase(os.path.abspath(folder.parent)) == os.path.normcase(os.path.abspath(inventories_dir())):
        return f"<data dir>/inventories/{folder.name}"  # default location: the name is a timestamp
    return "<custom output folder>"


def format_report(report: ScanReport, output_dir: Path, *, redact: bool = False) -> str:
    """Counts-only summary; the output folder is the only path shown (hidden with ``redact``)."""
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
    lines.append(f"  Results: {_show_output(output_dir, redact)}")
    lines.append("  Results contain your file names and paths: keep them private (see the README's privacy section).")
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
