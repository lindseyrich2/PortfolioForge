"""Possible duplicate copies and variants, from names, paths and sizes only.

Nothing is hidden or deleted: both sides stay in their own groups, linked by a
``possible_duplicate`` or ``possible_variant`` relationship for review.
Content hashing would be more reliable; it is a deferred, opt-in feature.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from typing import Iterable

from .config import GroupingConfig
from .features import is_generic_name, strip_copy_markers
from .index import InventoryIndex
from .models import CandidateGroup, Evidence, GroupKind, RelationKind, Relationship
from .scoring import stable_id

MAX_IDENTICAL_FILES = 10
"""Above this many identical (name, size) files, pairs are not reported (likely boilerplate)."""


def _relative_in_group(group: CandidateGroup, file_id: str, index: InventoryIndex) -> str:
    record = index.files[file_id]
    if group.anchor and group.summary.get("folder_unit"):
        anchor = index.folders[group.anchor]
        if anchor.rel and record.relative_path.startswith(anchor.rel + "/"):
            return record.relative_path[len(anchor.rel) + 1:].casefold()
        if not anchor.rel:
            return record.relative_path.casefold()
    return record.name.casefold()


def _anchor_name(group: CandidateGroup, index: InventoryIndex) -> str:
    return index.folders[group.anchor].name if group.anchor else group.working_title.text


def find_duplicates(groups: Iterable[CandidateGroup], index: InventoryIndex,
                    config: GroupingConfig) -> list[Relationship]:
    groups = [g for g in groups if g.kind is not GroupKind.COURSE_COLLECTION and g.members]
    found: list[Relationship] = []
    pair_seen: set[tuple[str, str]] = set()

    prints: dict[str, set[tuple[str, int]]] = {}
    names: dict[str, set[str]] = {}
    for g in groups:
        if len(g.members) < 2:
            continue
        rel = {m.file_id: _relative_in_group(g, m.file_id, index) for m in g.members}
        prints[g.id] = {(rel[f], index.files[f].size_bytes) for f in rel}
        names[g.id] = set(rel.values())
    inverted: dict[str, list[str]] = defaultdict(list)  # relative path → groups (sizes compared below)
    for gid, rel_names in names.items():
        for item in rel_names:
            inverted[item].append(gid)
    candidates = sorted({tuple(sorted(p)) for ids in inverted.values() if len(ids) <= 50
                         for p in combinations(sorted(set(ids)), 2)})
    by_id = {g.id: g for g in groups}
    for a_id, b_id in candidates:
        fa, fb = prints[a_id], prints[b_id]
        similarity = len(fa & fb) / len(fa | fb)
        name_similarity = len(names[a_id] & names[b_id]) / len(names[a_id] | names[b_id])
        if similarity >= config.t("duplicate_similarity"):
            kind = RelationKind.POSSIBLE_DUPLICATE
            ev = [Evidence("duplicate_layout", round(3.0 * similarity, 3),
                           f"{similarity:.0%} of files match by relative path and size", (a_id, b_id))]
        elif name_similarity >= config.t("variant_similarity"):
            kind = RelationKind.POSSIBLE_VARIANT
            ev = [Evidence("variant_layout", round(2.0 * name_similarity, 3),
                           f"{name_similarity:.0%} of file paths match, but sizes differ", (a_id, b_id))]
        else:
            continue
        a, b = by_id[a_id], by_id[b_id]
        copies = [g for g in (a, b) if strip_copy_markers(_anchor_name(g, index), config)[1]]
        if copies:
            ev.append(Evidence("copy_name", 1.0, "a folder name looks like a copy ('- Copy', '(2)', ...)",
                               tuple(g.id for g in copies)))
        found.append(Relationship(
            id=stable_id("r", kind.value, a_id, b_id), source=a_id, target=b_id, kind=kind,
            score=sum(e.weight for e in ev), evidence=tuple(ev), likely_original=_likely_original(a, b, index, config),
        ))
        pair_seen.add((a_id, b_id))

    # file-level duplicates across different groups (or unassigned)
    location = {m.file_id: g.id for g in groups for m in g.members}
    by_key: dict[tuple[str, int], list[str]] = defaultdict(list)
    for record in index.files.values():
        # generic names ("Untitled", "main") are too common to suggest a copy
        if record.size_bytes > 0 and not is_generic_name(record.stem, config):
            by_key[(record.name.casefold(), record.size_bytes)].append(record.id)
    for key, ids in sorted(by_key.items()):
        if not 2 <= len(ids) <= MAX_IDENTICAL_FILES:
            continue
        ids = sorted(ids, key=lambda f: index.files[f].relative_path)
        for x, y in combinations(ids, 2):
            gx, gy = location.get(x), location.get(y)
            if gx is not None and gx == gy:
                continue
            if gx and gy and tuple(sorted((gx, gy))) in pair_seen:
                continue
            found.append(Relationship(
                id=stable_id("r", "file_duplicate", x, y), source=f"file:{x}", target=f"file:{y}",
                kind=RelationKind.POSSIBLE_DUPLICATE, score=1.5,
                evidence=(Evidence("same_name_and_size", 1.5, "same file name and size", (x, y)),),
            ))
    return found


def _likely_original(a: CandidateGroup, b: CandidateGroup, index: InventoryIndex, config: GroupingConfig) -> str:
    """A suggestion only: no copy-style name, then earliest activity, then more files."""
    def key(g: CandidateGroup) -> tuple:
        is_copy = strip_copy_markers(_anchor_name(g, index), config)[1]
        start = g.time_span[0].timestamp() if g.time_span[0] else float("inf")
        return (is_copy, start, -len(g.members), g.id)
    return min((a, b), key=key).id
