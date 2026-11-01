"""Local storage for approvals, evidence packs, manifests and the audit log.

    <data dir>/understanding/<inventory id>/<group id>/
        file-approval-<id>.json   Gate 1 records
        evidence-<id>.json        redacted excerpts with source references
        manifest-<id>.json        what would be sent, and to whom
        payload-<id>.json         the exact payload bytes, for inspection
        disclosure-approval-<id>.json Gate 2 records
        audit.jsonl               every send attempt (no content)

Never inside a Git repository. Evidence and payloads contain excerpts of your
code: keep them private.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

from ..paths import data_dir, ensure_safe_output_dir
from .models import (
    DisclosureManifest,
    EvidencePack,
    FileApprovalRecord,
    ManifestApproval,
    approval_from_dict,
    dumps,
    manifest_approval_from_dict,
    manifest_from_dict,
    pack_from_dict,
    to_jsonable,
)

T = TypeVar("T")


def _safe_part(value: str) -> str:
    if not value or any(c in value for c in "/\\:") or value in (".", ".."):
        raise ValueError("Invalid identifier.")
    return value


def group_dir(inventory_key: str, group_id: str) -> Path:
    return data_dir() / "understanding" / _safe_part(inventory_key) / _safe_part(group_id)


def _write(folder: Path, name: str, text: str | bytes) -> Path:
    folder = ensure_safe_output_dir(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    temporary = folder / (name + ".tmp")
    if isinstance(text, bytes):
        temporary.write_bytes(text)
    else:
        temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    return path


def _latest(folder: Path, prefix: str, loader: Callable[[dict[str, Any]], T]) -> T | None:
    if not folder.is_dir():
        return None
    candidates = sorted((p for p in folder.glob(f"{prefix}-*.json")), key=lambda p: p.stat().st_mtime)
    for path in reversed(candidates):
        return loader(json.loads(path.read_text(encoding="utf-8")))
    return None


def save_file_approval(record: FileApprovalRecord) -> Path:
    return _write(group_dir(record.inventory_key, record.group_id), f"file-approval-{record.id}.json", dumps(record))


def latest_file_approval(inventory_key: str, group_id: str) -> FileApprovalRecord | None:
    return _latest(group_dir(inventory_key, group_id), "file-approval", approval_from_dict)


def save_pack(pack: EvidencePack) -> Path:
    return _write(group_dir(pack.inventory_key, pack.group_id), f"evidence-{pack.id}.json", dumps(pack))


def latest_pack(inventory_key: str, group_id: str) -> EvidencePack | None:
    return _latest(group_dir(inventory_key, group_id), "evidence", pack_from_dict)


def save_manifest(manifest: DisclosureManifest, payload: bytes) -> Path:
    folder = group_dir(manifest.inventory_key, manifest.group_id)
    _write(folder, f"payload-{manifest.id}.json", payload)
    return _write(folder, f"manifest-{manifest.id}.json", dumps(manifest))


def load_manifest(inventory_key: str, group_id: str, manifest_id: str) -> tuple[DisclosureManifest, bytes]:
    folder = group_dir(inventory_key, group_id)
    manifest = manifest_from_dict(json.loads((folder / f"manifest-{_safe_part(manifest_id)}.json")
                                             .read_text(encoding="utf-8")))
    return manifest, (folder / f"payload-{manifest_id}.json").read_bytes()


def latest_manifest(inventory_key: str, group_id: str) -> DisclosureManifest | None:
    return _latest(group_dir(inventory_key, group_id), "manifest", manifest_from_dict)


def save_manifest_approval(approval: ManifestApproval, manifest: DisclosureManifest) -> Path:
    return _write(group_dir(manifest.inventory_key, manifest.group_id),
                  f"disclosure-approval-{approval.id}.json", dumps(approval))


def find_manifest_approval(manifest: DisclosureManifest) -> ManifestApproval | None:
    folder = group_dir(manifest.inventory_key, manifest.group_id)
    if not folder.is_dir():
        return None
    for path in sorted(folder.glob("disclosure-approval-*.json")):
        approval = manifest_approval_from_dict(json.loads(path.read_text(encoding="utf-8")))
        if approval.manifest_id == manifest.id:
            return approval
    return None


def append_audit(inventory_key: str, group_id: str, entry: dict[str, Any]) -> Path:
    folder = ensure_safe_output_dir(group_dir(inventory_key, group_id))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "audit.jsonl"
    record = {"at": datetime.now(UTC).isoformat(), **to_jsonable(entry)}
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path
