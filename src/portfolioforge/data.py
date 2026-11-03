"""Manage locally stored PortfolioForge data: ``python -m portfolioforge.data``.

    list                      saved inventories (ids, dates, counts; no paths)
    stats [ID|latest]         counts-only statistics for one inventory
    purge ID... | --all | --older-than DAYS [--yes]
                              delete saved inventories (asks first)
    where                     show where data and settings are stored

``list`` and ``stats`` print counts only, never file names or paths, so their
output is safe to share. ``purge`` deletes only PortfolioForge's own output
files inside the data folder, never original project files.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Sequence

from .paths import config_dir, data_dir, drive_cache_dir, inventories_dir
from .privacy import default_privacy_file

EXIT_OK = 0
EXIT_ERROR = 1

#: The only files an inventory folder may contain (see inventory.sinks).
_INVENTORY_FILES = frozenset({"files.jsonl", "directories.jsonl", "skipped.jsonl", "report.json", "plan.json",
                              "drive-files.jsonl"})
#: Inventories of Google Drive, which "latest" leaves out so a Drive scan never takes over local work.
GOOGLE_DRIVE_MODE = "google_drive"
LATEST_DRIVE = "latest-drive"


@dataclass(frozen=True)
class StoredInventory:
    id: str
    path: Path
    report: dict | None
    """Parsed report.json, or None for an unfinished or unreadable write."""
    size_bytes: int

    @property
    def started_at(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.report["started_at"]) if self.report else None
        except (KeyError, TypeError, ValueError):
            return None


def stored_inventories(base: Path | None = None) -> list[StoredInventory]:
    """Inventory folders in the data folder, oldest first. Reads only report.json."""
    base = base or inventories_dir()
    if not base.is_dir():
        return []
    found = []
    with os.scandir(base) as entries:
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            path = Path(entry.path)
            report = None
            try:
                report = json.loads((path / "report.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            size = sum(
                f.stat(follow_symlinks=False).st_size
                for f in os.scandir(path)
                if f.is_file(follow_symlinks=False)
            )
            found.append(StoredInventory(entry.name, path, report, size))
    return sorted(found, key=lambda s: s.id)


def latest_finished(items: list[StoredInventory], *, drive: bool = False) -> StoredInventory | None:
    """The newest finished inventory: of Google Drive with ``drive``, otherwise of local files."""
    finished = [i for i in items if i.report is not None and (i.report.get("mode") == GOOGLE_DRIVE_MODE) == drive]
    return finished[-1] if finished else None


def format_list(items: list[StoredInventory]) -> str:
    if not items:
        return "No saved inventories."
    lines = [f"{'ID':<18} {'Schema':<6} {'Mode':<14} {'Complete':<9} {'Files':>9} {'On disk':>10}"]
    for item in items:
        r = item.report
        if r is None:
            lines.append(f"{item.id:<18} {'-':<6} {'(unfinished)':<14} {'-':<9} {'-':>9} {_mb(item.size_bytes):>10}")
            continue
        lines.append(
            f"{item.id:<18} {r.get('schema_version', '?')!s:<6} {r.get('mode', '?'):<14} "
            f"{'yes' if r.get('complete') else 'NO':<9} {r.get('summary', {}).get('total_files', 0):>9,} "
            f"{_mb(item.size_bytes):>10}"
        )
    return "\n".join(lines)


def format_stats(item: StoredInventory) -> str:
    """Aggregate counts only: no paths, folder names or file names."""
    r = item.report
    if r is None:
        return f"Inventory {item.id} is unfinished or unreadable (no report.json)."
    s = r.get("summary", {})
    origins = Counter(root.get("origin", "?") for root in r.get("roots", []))
    lines = [
        f"Inventory {item.id}",
        f"  Schema version:      {r.get('schema_version')}",
        f"  Mode:                {r.get('mode')}",
        f"  Complete:            {'yes' if r.get('complete') else 'NO'}"
        + (f" (stopped: {r['stop_reason']})" if r.get("stop_reason") else "")
        + (f" (limits reached: {', '.join(r['limits_reached'])})" if r.get("limits_reached") else ""),
        f"  Scan roots:          {sum(origins.values())} ({_join(origins)})",
        f"  Files inventoried:   {s.get('total_files', 0):,} ({s.get('total_size_bytes', 0) / 1_048_576:,.1f} MB)",
        f"  Folders visited:     {s.get('directories_scanned', 0):,}",
        f"  Entries skipped:     {s.get('skipped_entries', 0):,}",
        f"  Errors:              {s.get('errors', 0):,}" + (f" ({_join(s['errors_by_type'])})" if s.get("errors_by_type") else ""),
        f"  By category:         {_join(s.get('files_by_category', {}))}",
        f"  By language:         {_join(s.get('files_by_language', {}))}",
        f"  By extension:        {_join(s.get('files_by_extension', {}))}",
        f"  Skipped by reason:   {_join(s.get('skipped_by_reason', {}))}",
        f"  Unrecognized files:  {s.get('unrecognized_files', 0):,}",
        f"  Images not included: {s.get('excluded_image_files', 0):,}",
        f"  Cloud-only skipped:  {s.get('cloud_only_files_skipped', 0):,}",
    ]
    return "\n".join(lines)


def _join(counts: dict | Counter) -> str:
    items = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return ", ".join(f"{k} {v:,}" for k, v in items) or "none"


def _mb(size: int) -> str:
    return f"{size / 1_048_576:,.1f} MB"


def _resolve(items: list[StoredInventory], ref: str) -> StoredInventory | None:
    if ref in ("latest", LATEST_DRIVE):
        return latest_finished(items, drive=ref == LATEST_DRIVE)
    return next((i for i in items if i.id == ref), None)


class PurgeRefused(Exception):
    pass


def purge_inventory(item: StoredInventory, base: Path) -> None:
    """Delete one inventory folder, only if it holds nothing but PortfolioForge output."""
    path = item.path
    if path.parent.resolve() != base.resolve() or path.is_symlink():
        raise PurgeRefused(f"{item.id}: not a folder directly inside the inventories folder")
    entries = list(os.scandir(path))
    unexpected = [e for e in entries if e.name not in _INVENTORY_FILES or not e.is_file(follow_symlinks=False)]
    if unexpected:
        raise PurgeRefused(f"{item.id}: contains files PortfolioForge did not write; left untouched")
    for entry in entries:
        os.remove(entry.path)
    os.rmdir(path)
    cache = drive_cache_dir() / item.id  # copies of downloaded Drive files belong to their inventory
    if cache.is_dir() and not cache.is_symlink():
        shutil.rmtree(cache)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m portfolioforge.data",
        description="Manage PortfolioForge data stored on this computer. Never touches original files.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list saved inventories (no paths)")
    stats = sub.add_parser("stats", help="counts-only statistics for one inventory")
    stats.add_argument("inventory", nargs="?", default="latest", help="inventory ID or 'latest'")
    purge = sub.add_parser("purge", help="delete saved inventories")
    purge.add_argument("ids", nargs="*", metavar="ID")
    purge.add_argument("--all", action="store_true", help="delete every saved inventory")
    purge.add_argument("--older-than", type=int, metavar="DAYS", help="delete inventories older than DAYS")
    purge.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    sub.add_parser("where", help="show where data and settings are stored")
    return parser


def main(argv: Sequence[str] | None = None, *, input_fn: Callable[[str], str] = input) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(parser, args, input_fn)
    except OSError as exc:
        # Only the error type: OSError messages contain paths.
        print(f"error: could not read the data folder ({type(exc).__name__})", file=sys.stderr)
        return EXIT_ERROR


def _run(parser: argparse.ArgumentParser, args: argparse.Namespace, input_fn: Callable[[str], str]) -> int:
    base = inventories_dir()

    if args.command == "where":
        print(f"Data folder:        {data_dir()}")
        print(f"Inventories:        {base}")
        print(f"Drive downloads:    {drive_cache_dir()}")
        print(f"Settings folder:    {config_dir()}")
        print(f"Privacy settings:   {default_privacy_file()}")
        print("These folders contain personal file names and paths. Keep them private.")
        return EXIT_OK

    items = stored_inventories(base)
    if args.command == "list":
        print(format_list(items))
        return EXIT_OK

    if args.command == "stats":
        item = _resolve(items, args.inventory)
        if item is None:
            print(f"error: no inventory {args.inventory!r} (see: python -m portfolioforge.data list)", file=sys.stderr)
            return EXIT_ERROR
        print(format_stats(item))
        return EXIT_OK

    # purge
    chosen: list[StoredInventory]
    if args.all:
        chosen = items
    elif args.older_than is not None:
        cutoff = datetime.now(UTC) - timedelta(days=args.older_than)
        chosen = [i for i in items if i.started_at is not None and i.started_at < cutoff]
    elif args.ids:
        by_id = {i.id: i for i in items}
        missing = [ref for ref in args.ids if ref not in by_id]
        if missing:
            print(f"error: unknown inventory ID(s): {', '.join(missing)}", file=sys.stderr)
            return EXIT_ERROR
        chosen = [by_id[ref] for ref in args.ids]
    else:
        parser.error("purge needs inventory IDs, --all, or --older-than DAYS")
    if not chosen:
        print("Nothing to delete.")
        return EXIT_OK

    print("Will delete these saved inventories (PortfolioForge output only; original files are not touched):")
    print("\n".join(f"  {i.id}" for i in chosen))
    if not args.yes:
        try:
            answer = input_fn("Type 'delete' to confirm: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "delete":
            print("Nothing was deleted.")
            return EXIT_ERROR
    failed = False
    for item in chosen:
        try:
            purge_inventory(item, base)
            print(f"  deleted {item.id}")
        except (PurgeRefused, OSError) as exc:
            failed = True
            print(f"  skipped: {exc if isinstance(exc, PurgeRefused) else f'{item.id}: {type(exc).__name__}'}",
                  file=sys.stderr)
    return EXIT_ERROR if failed else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
