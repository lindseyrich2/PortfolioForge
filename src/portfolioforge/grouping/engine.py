"""Proposing candidate groups from a saved inventory. Pure: no file or network access.

``propose_groups`` reads only the in-memory Inventory (which may have been
loaded from its saved JSONL files) and returns a GroupingRun. It never opens
original project files, rescans folders, or contacts any service.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from typing import Iterable

from ..inventory.models import Inventory
from .config import GroupingConfig
from .duplicates import find_duplicates
from .features import file_role
from .flags import compute_flags
from .index import InventoryIndex
from .linking import TokenStats, find_relationships, nearest_groups
from .models import (
    GROUPING_SCHEMA_VERSION,
    CandidateGroup,
    Decision,
    GroupKind,
    GroupStatus,
    GroupingRun,
    Membership,
    UnassignedFile,
    WorkingTitle,
)
from .review import apply_group_decisions, apply_relationship_decisions
from .scoring import band, confidence, stable_id
from .segment import GroupBuilder, segment


class GroupingInvariantError(AssertionError):
    """Raised if a file would be lost or assigned twice. Indicates a bug."""


def inventory_reference(inventory: Inventory, key: str | None = None) -> dict:
    report = inventory.report
    digest = hashlib.sha256(
        "|".join([report.started_at.isoformat(), *(r.id for r in report.roots), str(len(inventory.files))]).encode()
    ).hexdigest()[:16]
    return {
        "key": key or f"inv_{digest}",
        "schema_version": report.schema_version,
        "started_at": report.started_at.isoformat(),
        "report_digest": digest,
        "files": len(inventory.files),
    }


def propose_groups(
    inventory: Inventory,
    config: GroupingConfig | None = None,
    decisions: Iterable[Decision] = (),
    *,
    inventory_key: str | None = None,
    now: datetime | None = None,
) -> GroupingRun:
    config = config or GroupingConfig()
    decisions = list(decisions)
    index = InventoryIndex(inventory)
    seg = segment(index, config)
    # groups that are a whole folder (not a cluster of loose files inside one)
    folder_units = {gid for gid, b in seg.groups.items() if b.anchor and gid == stable_id("g", "unit", b.anchor)}
    applied, stale = apply_group_decisions(seg.groups, seg.unassigned, decisions)

    groups = _finalize(seg.groups, index, config, folder_units)
    duplicates = find_duplicates(groups, index, config)
    copies = {frozenset((r.source, r.target)) for r in duplicates}
    # a duplicate pair is not also suggested as "related, consider merging"
    related = [r for r in find_relationships(groups, index, config) if frozenset((r.source, r.target)) not in copies]
    relationships = related + duplicates
    relationships, rel_applied, rel_stale = apply_relationship_decisions(relationships, groups, decisions)
    flags = compute_flags(groups, relationships, index, config)
    flag_ids: dict[str, list[str]] = {}
    for flag in flags:
        flag_ids.setdefault(flag.target, []).append(flag.id)
    groups = tuple(_with_flags(g, flag_ids.get(g.id, [])) for g in groups)

    stats = TokenStats([g for g in groups if g.kind is not GroupKind.COURSE_COLLECTION], index, config)
    unassigned = tuple(
        UnassignedFile(fid, reason, nearest_groups(index.files[fid], list(groups), index, config, stats))
        for fid, reason in sorted(seg.unassigned.items(), key=lambda kv: index.files[kv[0]].relative_path)
    )
    _check_every_file_once(inventory, groups, unassigned)

    kinds = Counter(g.kind.value for g in groups)
    run_stats = {
        "files": len(inventory.files),
        "grouped_files": sum(len(g.members) for g in groups),
        "unassigned_files": len(unassigned),
        "groups_by_kind": dict(sorted(kinds.items())),
        "groups_by_band": dict(sorted(Counter(g.band.value for g in groups
                                              if g.kind is not GroupKind.COURSE_COLLECTION).items())),
        "relationships_by_kind": dict(sorted(Counter(r.kind.value for r in relationships).items())),
        "flags_by_kind": dict(sorted(Counter(f.kind.value for f in flags).items())),
        "merge_suggestions": sum(r.suggest_merge for r in relationships),
    }
    return GroupingRun(
        schema_version=GROUPING_SCHEMA_VERSION,
        inventory_ref=inventory_reference(inventory, inventory_key),
        config_digest=config.digest(),
        created_at=now or datetime.now(UTC),
        groups=groups,
        relationships=tuple(relationships),
        flags=tuple(flags),
        unassigned=unassigned,
        decisions_applied=applied + rel_applied,
        stale_decisions=tuple(stale + rel_stale),
        stats=run_stats,
    )


def _finalize(builders: dict[str, GroupBuilder], index: InventoryIndex, config: GroupingConfig,
              folder_units: set[str]) -> tuple[CandidateGroup, ...]:
    # drop empty file groups, then collections that no longer hold anything
    live = {gid: b for gid, b in builders.items() if b.kind is GroupKind.COURSE_COLLECTION or b.members}
    while True:
        holding = {b.parent_id for b in live.values() if b.parent_id}
        empty = [gid for gid, b in live.items() if b.kind is GroupKind.COURSE_COLLECTION and gid not in holding]
        if not empty:
            break
        for gid in empty:
            del live[gid]
    for b in live.values():
        if b.parent_id not in live:
            b.parent_id = None

    children: dict[str, list[str]] = {}
    for b in live.values():
        if b.parent_id:
            children.setdefault(b.parent_id, []).append(b.id)

    def descendant_files(gid: str) -> list[str]:
        result = list(live[gid].members)
        for c in children.get(gid, []):
            result.extend(descendant_files(c))
        return result

    finalized = []
    for b in live.values():
        records = [index.files[f] for f in (descendant_files(b.id) if b.kind is GroupKind.COURSE_COLLECTION
                                            else b.members)]
        times = [r.modified_at for r in records if r.modified_at]
        members = tuple(sorted(
            (Membership(fid, file_role(index.files[fid].category.value, index.files[fid].name),
                        round(confidence(ev, config), 4), tuple(ev))
             for fid, ev in b.members.items()),
            key=lambda m: index.files[m.file_id].relative_path,
        ))
        value = confidence(b.evidence, config)
        finalized.append(CandidateGroup(
            id=b.id, kind=b.kind, working_title=WorkingTitle(b.title), anchor=b.anchor, parent_id=b.parent_id,
            members=members, confidence=round(value, 4), band=band(value, config), evidence=tuple(b.evidence),
            course_hints=b.course_hints, time_span=(min(times) if times else None, max(times) if times else None),
            summary={
                "files": len(records),
                "size_bytes": sum(r.size_bytes for r in records),
                "by_category": dict(sorted(Counter(r.category.value for r in records).items())),
                "by_language": dict(sorted(Counter(r.language for r in records if r.language).items())),
                "child_groups": len(children.get(b.id, [])),
                "folder_unit": b.id in folder_units,
                "location": index.display_path(b.anchor) if b.anchor else None,
            },
            status=GroupStatus.ACCEPTED if b.status == "accepted" else GroupStatus.PROPOSED,
            user_title=b.user_title, labels=tuple(b.labels),
        ))
    return tuple(sorted(finalized, key=lambda g: (g.summary.get("location") or "", g.kind.value, g.id)))


def _with_flags(group: CandidateGroup, flag_ids: list[str]) -> CandidateGroup:
    return replace(group, flag_ids=tuple(sorted(flag_ids))) if flag_ids else group


def _check_every_file_once(inventory: Inventory, groups: Iterable[CandidateGroup],
                           unassigned: Iterable[UnassignedFile]) -> None:
    seen: Counter[str] = Counter()
    for g in groups:
        seen.update(m.file_id for m in g.members)
    seen.update(u.file_id for u in unassigned)
    expected = {f.id for f in inventory.files}
    duplicated = [f for f, c in seen.items() if c > 1]
    missing = expected - set(seen)
    unknown = set(seen) - expected
    if duplicated or missing or unknown:
        raise GroupingInvariantError(
            f"grouping invariant broken: {len(duplicated)} duplicated, {len(missing)} missing, {len(unknown)} unknown"
        )
