"""Downloading approved Drive files into a private local cache, so the normal reader can read them.

This runs only after the typed read approval, and only for the files that approval lists. Each file
is checked against what discovery recorded, then given Drive's modification time, so the reader's
own "unchanged since approved" check applies as it does to local files. Nothing else changes: the
AI disclosure gate, code validation and publishing work on the cached copy like any other file.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable

from ..inventory import rules
from ..inventory.models import FileRecord, Inventory, ScanMode
from ..inventory.roots import is_within, normalize_path
from ..paths import drive_cache_dir, ensure_safe_output_dir
from .auth import CONTENT_SCOPE, DriveError, load_credentials
from .client import DriveClient
from .discovery import DriveFileRef, load_refs

READ_NOTICE = ("These files would be downloaded read-only from Google Drive to a private cache on this computer, and "
               "read there. Nothing in your Drive changes, and nothing is sent to an AI provider, run or published.")


class DriveFileChanged(DriveError):
    pass


def is_drive_inventory(inventory: Inventory) -> bool:
    return inventory.report.mode is ScanMode.GOOGLE_DRIVE


def make_client() -> DriveClient:
    """The only place the download permission is loaded. Replaced in tests."""
    return DriveClient(load_credentials(CONTENT_SCOPE))


def require_download_permission(inventory: Inventory) -> None:
    """Fail before asking for approval, not after. Does nothing for a local inventory."""
    if is_drive_inventory(inventory):
        make_client()


def _matches(data: bytes, record: FileRecord, ref: DriveFileRef) -> bool:
    if len(data) != record.size_bytes:
        return False
    return ref.md5 is None or hashlib.md5(data, usedforsecurity=False).hexdigest() == ref.md5


def _cache_path(record: FileRecord, inventory_key: str) -> Path:
    """The inventory file says where to write, so that is checked instead of trusted."""
    cache = drive_cache_dir() / inventory_key
    if not is_within(normalize_path(record.absolute_path), normalize_path(str(cache))):
        raise DriveError("A Drive inventory record points outside the download cache, so it was refused.")
    return Path(record.absolute_path)


def download_approved(inventory: Inventory, inventory_key: str, file_ids: Iterable[str],
                      client: DriveClient | None = None) -> int:
    """Download these files if they aren't cached yet. Does nothing for a local inventory. Safe to repeat."""
    if not is_drive_inventory(inventory):
        return 0
    wanted = list(dict.fromkeys(file_ids))
    records = {f.id: f for f in inventory.files}
    refs = load_refs(inventory_key)
    if any(f not in records or f not in refs for f in wanted):
        raise DriveError("An approved file is not in this Drive inventory. Run the Drive scan again.")
    if any(rules.is_sensitive_name(records[f].name) for f in wanted):
        raise DriveError("Sensitive file names are never downloaded.")
    client = client or make_client()
    downloaded = 0
    for file_id in wanted:
        record, ref = records[file_id], refs[file_id]
        path = _cache_path(record, inventory_key)
        if path.is_file() and not path.is_symlink() and _matches(path.read_bytes(), record, ref):
            continue
        data = client.download(ref.drive_id, record.size_bytes)
        if not _matches(data, record, ref):
            raise DriveFileChanged("A Drive file changed after it was discovered, so it wasn't kept. Run the Drive "
                                   "scan again and approve the current version.")
        ensure_safe_output_dir(path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".part")
        temporary.write_bytes(data)
        os.replace(temporary, path)
        if record.modified_at:
            stamp = record.modified_at.timestamp()
            os.utime(path, (stamp, stamp))
        downloaded += 1
    return downloaded
