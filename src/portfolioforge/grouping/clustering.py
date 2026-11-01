"""Splitting loose files in one folder into assignments.

Files are linked when name evidence (same assignment number, same base name,
shared tokens) and time evidence add up past a threshold. Files with
different assignment numbers are never linked. Generic names (``Untitled``,
``main``) contribute no name evidence, and time evidence is ignored when a
folder's timestamps look bulk-copied (almost all identical).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..inventory.models import FileRecord
from .config import GroupingConfig
from .features import AssignmentMarker, assignment_marker, distinctive_tokens, is_generic_name, strip_copy_markers
from .models import Evidence, GroupKind
from .scoring import stable_id, strongest_by_kind

if TYPE_CHECKING:
    from .index import Folder
    from .segment import GroupBuilder

_CODE_LIKE = frozenset({"code", "notebook"})
_SUPPORT = frozenset({"document", "presentation", "data", "spreadsheet"})


@dataclass(frozen=True)
class _Item:
    record: FileRecord
    marker: AssignmentMarker | None
    base: str
    tokens: frozenset[str]
    generic: bool
    time: float | None


def uniform_timestamps(times: list[float], config: GroupingConfig) -> bool:
    """True if almost all files share (nearly) the same modification time, as after a bulk copy."""
    if len(times) < config.uniform_time_min_files:
        return False
    times = sorted(times)
    window = config.uniform_time_window_seconds
    best, start = 0, 0
    for end in range(len(times)):
        while times[end] - times[start] > window:
            start += 1
        best = max(best, end - start + 1)
    return best / len(times) >= config.uniform_time_fraction


def _items(files: list[FileRecord], config: GroupingConfig) -> list[_Item]:
    items = []
    for f in files:
        base, _ = strip_copy_markers(f.stem, config)
        items.append(_Item(
            record=f,
            marker=assignment_marker(f.stem, config),
            base=base.casefold(),
            tokens=distinctive_tokens(f.stem, config),
            generic=is_generic_name(f.stem, config),
            time=f.modified_at.timestamp() if f.modified_at else None,
        ))
    return items


def pair_evidence(a: _Item, b: _Item, config: GroupingConfig, use_time: bool) -> list[Evidence]:
    refs = (a.record.id, b.record.id)
    ev: list[Evidence] = []
    if a.marker and b.marker and a.marker.number and b.marker.number:
        if a.marker.key == b.marker.key:
            ev.append(Evidence("same_assignment_number", config.w("same_assignment_number"),
                               f"both named as assignment '{a.marker.key}'", refs))
        else:
            ev.append(Evidence("different_assignment_number", config.w("different_assignment_number"),
                               f"different assignments ('{a.marker.key}' vs '{b.marker.key}')", refs))
            return ev
    if not (a.generic or b.generic):
        if a.base == b.base and a.record.extension != b.record.extension:
            ev.append(Evidence("same_base_name", config.w("same_base_name"), f"same base name '{a.base}'", refs))
        shared = a.tokens & b.tokens
        if shared:
            weight = min(config.w("shared_token") * len(shared), config.w("shared_token_cap"))
            ev.append(Evidence("shared_token", weight, f"shared name words: {', '.join(sorted(shared))}", refs))
    if use_time and a.time is not None and b.time is not None:
        gap = abs(a.time - b.time)
        if gap <= config.tight_time_window_seconds:
            ev.append(Evidence("very_close_in_time", config.w("very_close_in_time"), "modified within an hour", refs))
        elif gap <= config.cluster_time_window_seconds and not (a.generic or b.generic):
            ev.append(Evidence("close_in_time", config.w("close_in_time"), "modified within a few days", refs))
    if ev and {a.record.category.value, b.record.category.value} & _CODE_LIKE \
            and {a.record.category.value, b.record.category.value} & _SUPPORT:
        ev.append(Evidence("complementary_types", config.w("complementary_types"),
                           "code or notebook next to a report or data file", refs))
    return ev


def cluster_loose_files(folder: Folder, parent_id: str | None, config: GroupingConfig,
                        course_hints: tuple[str, ...] = ()) -> list[GroupBuilder]:
    from .segment import GroupBuilder  # avoid an import cycle

    files = folder.files
    if not files:
        return []
    items = _items(files, config)
    times = [i.time for i in items if i.time is not None]
    use_time = not uniform_timestamps(times, config)
    n = len(items)
    parent = list(range(n))
    keys: list[set[str]] = [{i.marker.key} if i.marker and i.marker.number else set() for i in items]
    edges: dict[int, list[Evidence]] = {k: [] for k in range(n)}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pairs: list[tuple[float, int, int, list[Evidence]]] = []
    if n <= config.max_files_per_cluster_folder:
        for i in range(n):
            for j in range(i + 1, n):
                ev = pair_evidence(items[i], items[j], config, use_time)
                score = sum(e.weight for e in ev)
                if score >= config.t("cluster_link"):
                    pairs.append((score, i, j, ev))
    else:  # very large folders: only exact assignment keys and base names
        buckets: dict[str, list[int]] = {}
        for k, item in enumerate(items):
            key = item.marker.key if item.marker and item.marker.number else ("base:" + item.base if not item.generic else "")
            if key:
                buckets.setdefault(key, []).append(k)
        for members in buckets.values():
            for a, b in zip(members, members[1:]):
                ev = pair_evidence(items[a], items[b], config, use_time=False)
                if sum(e.weight for e in ev) >= config.t("cluster_link"):
                    pairs.append((sum(e.weight for e in ev), a, b, ev))
    for _, i, j, ev in sorted(pairs, key=lambda p: (-p[0], p[1], p[2])):
        ri, rj = find(i), find(j)
        if ri == rj:
            edges[i].extend(ev)
            edges[j].extend(ev)
            continue
        if keys[ri] and keys[rj] and keys[ri] != keys[rj]:
            continue  # never join different assignments
        parent[rj] = ri
        keys[ri] |= keys[rj]
        edges[i].extend(ev)
        edges[j].extend(ev)

    components: dict[int, list[int]] = {}
    for k in range(n):
        components.setdefault(find(k), []).append(k)

    builders: list[GroupBuilder] = []
    leftovers: list[int] = []
    folder_ev = Evidence("loose_in_folder", 1.0, f"loose files in '{folder.name}'", (folder.id,))
    for root, members in sorted(components.items(), key=lambda kv: min(items[m].record.relative_path for m in kv[1])):
        marker_items = [items[m] for m in members if items[m].marker]
        numbered = [i for i in marker_items if i.marker and i.marker.number]
        if len(members) == 1 and not numbered:
            leftovers.extend(members)
            continue
        key = numbered[0].marker.key if numbered else None  # type: ignore[union-attr]
        seed = key or min(items[m].base for m in members)
        group_ev = [folder_ev]
        if key:
            group_ev.append(Evidence("assignment_name", config.w("assignment_name"),
                                     f"files named as assignment '{key}'", tuple(i.record.id for i in numbered)))
        group_ev.extend(strongest_by_kind(e for m in members for e in edges[m]))
        title = key.replace("-", " ") if key else _common_title(items, members)
        builder = GroupBuilder(
            id=stable_id("g", "cluster", folder.id, seed),
            kind=GroupKind.ASSIGNMENT if key else GroupKind.PROJECT,
            title=title, anchor=folder.id, parent_id=parent_id, evidence=group_ev, course_hints=course_hints,
        )
        for m in members:
            member_ev = strongest_by_kind(edges[m]) or (
                [Evidence("assignment_name", config.w("assignment_name"), f"named as assignment '{key}'",
                          (items[m].record.id,))] if key else []
            )
            builder.add(items[m].record, [folder_ev, *member_ev])
        builders.append(builder)

    if len(leftovers) >= 2:
        loose = GroupBuilder(
            id=stable_id("g", "loose", folder.id), kind=GroupKind.LOOSE_COLLECTION,
            title=f"Loose files in {folder.name}", anchor=folder.id, parent_id=parent_id, course_hints=course_hints,
            evidence=[Evidence("loose_in_folder", 0.5,
                               f"share the folder '{folder.name}', no other grouping evidence", (folder.id,))],
        )
        for m in leftovers:
            loose.add(items[m].record, [Evidence("loose_in_folder", 0.5, "only shares a folder", (folder.id,))])
        builders.append(loose)
    return builders


def _common_title(items: list[_Item], members: list[int]) -> str:
    token_sets = [items[m].tokens for m in members if items[m].tokens]
    if token_sets:
        common = frozenset.intersection(*token_sets) if len(token_sets) > 1 else token_sets[0]
        if common:
            return " ".join(sorted(common))
    return min(items[m].base for m in members)
