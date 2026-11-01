"""Suggested relationships between groups in different folders. Never merges.

Two groups are related when they share *rare* name words (weighted by how
uncommon they are across the whole inventory), a course code, and/or
overlapping activity in time. A document-heavy group linked to a code group
becomes a ``report_for`` suggestion. Strong links carry a merge *suggestion*
that only the user can act on.
"""

from __future__ import annotations

import math
from collections import defaultdict
from itertools import combinations
from typing import Iterable

from ..inventory.models import FileRecord
from .config import GroupingConfig
from .features import distinctive_tokens
from .index import InventoryIndex
from .models import CandidateGroup, Evidence, GroupKind, RelationKind, Relationship
from .scoring import stable_id

_DOC = frozenset({"document", "presentation"})
_CODE = frozenset({"code", "notebook"})


class TokenStats:
    """Distinctive name tokens per group, with inverse document frequency."""

    def __init__(self, groups: list[CandidateGroup], index: InventoryIndex, config: GroupingConfig) -> None:
        self.tokens: dict[str, frozenset[str]] = {}
        for g in groups:
            words = set(distinctive_tokens(g.working_title.text, config))
            if g.anchor and g.summary.get("folder_unit"):
                words |= distinctive_tokens(index.folders[g.anchor].name, config)
            for m in g.members:
                words |= distinctive_tokens(index.files[m.file_id].stem, config)
            self.tokens[g.id] = frozenset(words)
        df: dict[str, int] = defaultdict(int)
        for words in self.tokens.values():
            for w in words:
                df[w] += 1
        n = max(len(groups), 1)
        limit = max(2, int(config.t("max_token_document_ratio") * n))
        # 1 for a word shared by every candidate that survives the limit, more for rarer words
        self.idf = {w: 1.0 + math.log(n / c) for w, c in df.items() if c <= limit}

    def rare(self, group_id: str) -> frozenset[str]:
        return frozenset(w for w in self.tokens.get(group_id, ()) if w in self.idf)


def _span(g: CandidateGroup) -> tuple[float, float] | None:
    start, end = g.time_span
    return (start.timestamp(), end.timestamp()) if start and end else None


def _share(g: CandidateGroup, kinds: frozenset[str]) -> float:
    by_cat = g.summary.get("by_category", {})
    total = sum(by_cat.values()) or 1
    return sum(v for k, v in by_cat.items() if k in kinds) / total


def _lineage(g: CandidateGroup, by_id: dict[str, CandidateGroup]) -> set[str]:
    seen, current = set(), g.parent_id
    while current and current not in seen:
        seen.add(current)
        current = by_id[current].parent_id if current in by_id else None
    return seen


def find_relationships(groups: Iterable[CandidateGroup], index: InventoryIndex,
                       config: GroupingConfig) -> list[Relationship]:
    candidates = [g for g in groups if g.kind is not GroupKind.COURSE_COLLECTION and g.members]
    by_id = {g.id: g for g in groups}
    stats = TokenStats(candidates, index, config)
    inverted: dict[str, list[str]] = defaultdict(list)
    for g in candidates:
        for w in stats.rare(g.id):
            inverted["t:" + w].append(g.id)
        for c in g.course_hints:
            inverted["c:" + c].append(g.id)
    pairs = sorted({tuple(sorted(p)) for ids in inverted.values() for p in combinations(sorted(ids), 2)})

    found: list[Relationship] = []
    for a_id, b_id in pairs:
        a, b = by_id[a_id], by_id[b_id]
        if a.anchor == b.anchor or a_id in _lineage(b, by_id) or b_id in _lineage(a, by_id):
            continue
        ev: list[Evidence] = []
        shared = stats.rare(a_id) & stats.rare(b_id)
        if shared:
            weight = min(sum(stats.idf[w] for w in shared) * config.w("rare_shared_token"),
                         config.w("rare_shared_token_cap"))
            ev.append(Evidence("rare_shared_token", weight, f"share uncommon name words: {', '.join(sorted(shared))}",
                               (a_id, b_id)))
        courses = set(a.course_hints) & set(b.course_hints)
        same_collection = a.parent_id is not None and a.parent_id == b.parent_id
        if courses and not same_collection:
            ev.append(Evidence("same_course_code", config.w("same_course_code"),
                               f"same course code ({', '.join(sorted(courses))})", (a_id, b_id)))
        if not ev:
            continue
        sa, sb = _span(a), _span(b)
        window = config.relationship_time_window_seconds
        if sa and sb and sa[0] - window <= sb[1] and sb[0] - window <= sa[1]:
            ev.append(Evidence("overlapping_time", config.w("overlapping_time"), "active around the same time",
                               (a_id, b_id)))
        report = (_share(a, _DOC) >= 0.8 and _share(b, _CODE) >= 0.5) or (_share(b, _DOC) >= 0.8 and _share(a, _CODE) >= 0.5)
        if report and shared:
            ev.append(Evidence("report_and_code", config.w("report_and_code"),
                               "a document group next to a code group", (a_id, b_id)))
        score = sum(e.weight for e in ev)
        if shared:
            kind = RelationKind.REPORT_FOR if report else RelationKind.RELATED_TO
            if score < config.t("relationship"):
                continue
        else:
            kind = RelationKind.SAME_COURSE
            if score < config.t("same_course_relationship"):
                continue
        found.append(Relationship(
            id=stable_id("r", kind.value, a_id, b_id), source=a_id, target=b_id, kind=kind, score=score,
            evidence=tuple(ev),
            suggest_merge=kind in (RelationKind.RELATED_TO, RelationKind.REPORT_FOR)
            and score >= config.t("merge_suggestion"),
        ))
    return found


def nearest_groups(record: FileRecord, groups: list[CandidateGroup], index: InventoryIndex,
                   config: GroupingConfig, stats: TokenStats | None = None, limit: int = 3) -> tuple[tuple[str, float], ...]:
    """Groups an unassigned file might belong to, by shared uncommon name words."""
    words = distinctive_tokens(record.stem, config)
    if not words:
        return ()
    stats = stats or TokenStats([g for g in groups if g.kind is not GroupKind.COURSE_COLLECTION], index, config)
    scored = []
    for g in groups:
        if g.kind is GroupKind.COURSE_COLLECTION:
            continue
        shared = words & stats.rare(g.id)
        if shared:
            scored.append((g.id, round(sum(stats.idf[w] for w in shared), 3)))
    return tuple(sorted(scored, key=lambda s: (-s[1], s[0]))[:limit])
