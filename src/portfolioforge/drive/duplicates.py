"""Possible duplicates between a Drive inventory and a local one. Metadata only, and only ever a suggestion.

Both inventories are grouped together in memory by the existing engine, so "duplicate" means what it
means everywhere else: matching paths and sizes. Notebooks get one more check, because Colab rewrites
a notebook when saving it: the same distinctive name at a different size is a possible variant. No
local file is opened, no project is hidden or removed, and the batch shows the result as a concern.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from ..grouping.config import GroupingConfig
from ..grouping.engine import propose_groups
from ..grouping.features import is_generic_name
from ..grouping.models import GroupingRun, RelationKind
from ..inventory.models import FileRecord, Inventory
from ..paths import data_dir, ensure_safe_output_dir

MAX_NAME_MATCHES = 10
"""Above this many files with one name, a match says nothing (likely boilerplate)."""
_DUPLICATE, _VARIANT = RelationKind.POSSIBLE_DUPLICATE.value, RelationKind.POSSIBLE_VARIANT.value
_NOTEBOOK = "notebook"
_LOOSE_FILE = "file:"


@dataclass(frozen=True)
class CrossSourceMatch:
    drive_group: str
    local_group: str
    kind: str
    detail: str


def _merged(drive: Inventory, local: Inventory) -> Inventory:
    return Inventory(replace(drive.report, roots=(*drive.report.roots, *local.report.roots)),
                     (*drive.files, *local.files), (*drive.directories, *local.directories),
                     (*drive.skipped, *local.skipped))


def _file_groups(run: GroupingRun) -> dict[str, str]:
    return {m.file_id: g.id for g in run.groups for m in g.members}


def find_matches(drive: Inventory, drive_run: GroupingRun, local: Inventory, local_run: GroupingRun,
                 config: GroupingConfig | None = None) -> list[CrossSourceMatch]:
    """Strongest match for each pair of groups, named by the group IDs each inventory's own run uses."""
    config = config or GroupingConfig()
    drive_files, local_files = _file_groups(drive_run), _file_groups(local_run)
    for record in local.files:  # a loose local file isn't in any group, and is still worth mentioning
        local_files.setdefault(record.id, f"{_LOOSE_FILE}{record.id}")
    merged = propose_groups(_merged(drive, local), config)
    members = {g.id: [m.file_id for m in g.members] for g in merged.groups}
    found: dict[tuple[str, str], CrossSourceMatch] = {}

    def record(left: list[str], right: list[str], kind: str, detail: str) -> None:
        for one, other in ((left, right), (right, left)):
            drive_side = Counter(drive_files[f] for f in one if f in drive_files)
            local_side = Counter(local_files[f] for f in other if f in local_files)
            if drive_side and local_side:
                pair = (drive_side.most_common(1)[0][0], local_side.most_common(1)[0][0])
                existing = found.get(pair)
                if existing is None or (existing.kind == _VARIANT and kind == _DUPLICATE):
                    found[pair] = CrossSourceMatch(*pair, kind, detail)

    for relation in merged.relationships:
        if relation.kind.value not in (_DUPLICATE, _VARIANT):
            continue
        sides = [[side[len("file:"):]] if side.startswith("file:") else members.get(side, [])
                 for side in (relation.source, relation.target)]
        record(sides[0], sides[1], relation.kind.value, relation.evidence[0].detail)

    for left, right in _same_notebook_names(drive.files, local.files, config):
        record([left], [right], _VARIANT, "a notebook with the same name, but a different size")
    return sorted(found.values(), key=lambda m: (m.drive_group, m.local_group))


def _same_notebook_names(drive: tuple[FileRecord, ...], local: tuple[FileRecord, ...],
                         config: GroupingConfig) -> list[tuple[str, str]]:
    by_name: dict[str, list[FileRecord]] = defaultdict(list)
    for record in local:
        if record.category.value == _NOTEBOOK and not is_generic_name(record.stem, config):
            by_name[record.name.casefold()].append(record)
    pairs = []
    for record in drive:
        others = by_name.get(record.name.casefold(), [])
        if record.category.value == _NOTEBOOK and 0 < len(others) <= MAX_NAME_MATCHES:
            pairs.extend((record.id, other.id) for other in others if other.size_bytes != record.size_bytes)
    return pairs


def _concerns_path(inventory_key: str) -> Path:
    if not inventory_key or any(sep in inventory_key for sep in ("/", "\\")) or inventory_key in (".", ".."):
        raise ValueError("Invalid inventory id.")
    return data_dir() / "cross-source" / f"{inventory_key}.json"


def save_matches(drive_key: str, local_key: str, matches: list[CrossSourceMatch]) -> None:
    """Saved for both inventories, so each batch mentions the other. Only group IDs, so --redact stays safe."""
    label = {_DUPLICATE: "possible duplicate", _VARIANT: "possible variant"}
    sides: dict[str, dict[str, list[str]]] = {drive_key: defaultdict(list), local_key: defaultdict(list)}
    for m in matches:
        loose = m.local_group.startswith(_LOOSE_FILE)
        other = f"local file {m.local_group[len(_LOOSE_FILE):]}" if loose else f"local project {m.local_group}"
        sides[drive_key][m.drive_group].append(f"{label[m.kind]} of {other}")
        if not loose:
            sides[local_key][m.local_group].append(f"{label[m.kind]} of Google Drive project {m.drive_group}")
    for key, concerns in sides.items():
        path = _concerns_path(key)
        ensure_safe_output_dir(path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps({"matches": [asdict(m) for m in matches], "concerns": concerns}, indent=2),
                             encoding="utf-8")
        temporary.replace(path)


def load_concerns(inventory_key: str) -> dict[str, list[str]]:
    """Group ID to concerns. Advisory: an absent or unreadable file just means none are known."""
    try:
        return json.loads(_concerns_path(inventory_key).read_text(encoding="utf-8"))["concerns"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
