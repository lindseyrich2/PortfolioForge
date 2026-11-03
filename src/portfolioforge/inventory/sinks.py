"""Where scan records go: memory (small scans, tests) or local JSONL files."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Iterable, Protocol

from ..paths import ensure_safe_output_dir, inventories_dir
from .models import (
    DirectoryRecord,
    FileRecord,
    Inventory,
    ScanPlan,
    ScanReport,
    SkippedEntry,
    to_jsonable,
)

FILES_NAME = "files.jsonl"
DIRECTORIES_NAME = "directories.jsonl"
SKIPPED_NAME = "skipped.jsonl"
REPORT_NAME = "report.json"
PLAN_NAME = "plan.json"
#: Only in a Google Drive inventory: how to fetch each file once it has been approved.
DRIVE_FILES_NAME = "drive-files.jsonl"
#: Only in a GitHub inventory: each repository's ownership, commit counts and attribution label.
GITHUB_REPOS_NAME = "github-repos.json"


class InventorySink(Protocol):
    def add_file(self, record: FileRecord) -> None: ...
    def add_directory(self, record: DirectoryRecord) -> None: ...
    def add_skipped(self, entry: SkippedEntry) -> None: ...
    def close(self, report: ScanReport) -> None: ...


def build_inventory(
    report: ScanReport,
    files: Iterable[FileRecord],
    directories: Iterable[DirectoryRecord],
    skipped: Iterable[SkippedEntry],
) -> Inventory:
    """Assemble an Inventory with records in a deterministic order."""
    order = {root.id: index for index, root in enumerate(report.roots)}

    def position(root_id: str | None) -> int:
        return order.get(root_id or "", len(order))

    return Inventory(
        report=report,
        files=tuple(sorted(files, key=lambda r: (position(r.root_id), r.relative_path))),
        directories=tuple(sorted(directories, key=lambda r: (position(r.root_id), r.relative_path))),
        skipped=tuple(
            sorted(skipped, key=lambda s: (position(s.root_id), s.relative_path, s.reason.value))
        ),
    )


class MemorySink:
    """Keeps every record in memory. Suited to targeted scans and tests."""

    def __init__(self) -> None:
        self.files: list[FileRecord] = []
        self.directories: list[DirectoryRecord] = []
        self.skipped: list[SkippedEntry] = []
        self.report: ScanReport | None = None

    def add_file(self, record: FileRecord) -> None:
        self.files.append(record)

    def add_directory(self, record: DirectoryRecord) -> None:
        self.directories.append(record)

    def add_skipped(self, entry: SkippedEntry) -> None:
        self.skipped.append(entry)

    def close(self, report: ScanReport) -> None:
        self.report = report

    def inventory(self) -> Inventory:
        if self.report is None:
            raise RuntimeError("The scan has not finished.")
        return build_inventory(self.report, self.files, self.directories, self.skipped)


class JsonlSink:
    """Streams records to JSON Lines files in a local output folder.

    Memory use stays flat regardless of scan size. ``report.json`` is written
    last, so a folder without it holds an interrupted write.
    """

    def __init__(self, output_dir: str | os.PathLike[str]) -> None:
        self.output_dir = ensure_safe_output_dir(output_dir)
        if (self.output_dir / REPORT_NAME).exists():
            raise FileExistsError(f"An inventory already exists in {self.output_dir}")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._handles: dict[str, IO[str]] = {}
        try:
            for name in (FILES_NAME, DIRECTORIES_NAME, SKIPPED_NAME):
                self._handles[name] = open(self.output_dir / name, "w", encoding="utf-8", newline="\n")
        except BaseException:
            self._close_handles()
            raise

    def __enter__(self) -> JsonlSink:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._close_handles()

    def _write(self, name: str, record: Any) -> None:
        self._handles[name].write(json.dumps(to_jsonable(record), ensure_ascii=False) + "\n")

    def add_file(self, record: FileRecord) -> None:
        self._write(FILES_NAME, record)

    def add_directory(self, record: DirectoryRecord) -> None:
        self._write(DIRECTORIES_NAME, record)

    def add_skipped(self, entry: SkippedEntry) -> None:
        self._write(SKIPPED_NAME, entry)

    def write_plan(self, plan: ScanPlan) -> None:
        _write_json(self.output_dir / PLAN_NAME, plan.to_dict())

    def close(self, report: ScanReport) -> None:
        self._close_handles()
        _write_json(self.output_dir / REPORT_NAME, to_jsonable(report))

    def _close_handles(self) -> None:
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def load_inventory(directory: str | os.PathLike[str]) -> Inventory:
    """Load an inventory written by :class:`JsonlSink`."""
    directory = Path(directory)
    report_path = directory / REPORT_NAME
    if not report_path.exists():
        raise FileNotFoundError(f"No finished inventory in {directory} (missing {REPORT_NAME})")
    report = ScanReport.from_dict(json.loads(report_path.read_text(encoding="utf-8")))
    return build_inventory(
        report,
        (FileRecord.from_dict(d) for d in _read_jsonl(directory / FILES_NAME)),
        (DirectoryRecord.from_dict(d) for d in _read_jsonl(directory / DIRECTORIES_NAME)),
        (SkippedEntry.from_dict(d) for d in _read_jsonl(directory / SKIPPED_NAME)),
    )


INVENTORY_FILE_NAMES = frozenset({FILES_NAME, DIRECTORIES_NAME, SKIPPED_NAME, REPORT_NAME, PLAN_NAME, DRIVE_FILES_NAME,
                                  GITHUB_REPOS_NAME})


def default_output_dir(now: datetime | None = None) -> Path:
    """``<data dir>/inventories/<UTC timestamp>`` (see :mod:`portfolioforge.paths`)."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return inventories_dir() / stamp
