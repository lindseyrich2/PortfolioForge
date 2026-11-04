"""Persistent user review decisions, applied as corrections on every run.

Decisions live in a local ledger (one JSON object per line) outside any Git
repository. Group and relationship ids are stable, so the same decisions are
re-applied whenever proposals are regenerated. A decision whose targets no
longer exist is reported as stale, never silently dropped.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from ..paths import data_dir, ensure_safe_output_dir
from .models import CandidateGroup, Decision, Evidence, GroupKind, RelationKind, Relationship, to_jsonable
from .scoring import stable_id
from .segment import GroupBuilder

GROUP_ACTIONS = frozenset({"accept", "reject", "rename", "merge", "split", "move", "unassign", "label", "unlabel"})
RELATIONSHIP_ACTIONS = frozenset({"confirm", "dismiss", "keep_both", "mark_duplicate"})
LABELS = frozenset({"mine", "third_party", "ignored"})
DECISIONS_FILE = "decisions.jsonl"


class DecisionError(ValueError):
    pass


def reviews_dir(inventory_key: str) -> Path:
    return data_dir() / "reviews" / inventory_key


def decisions_path(inventory_key: str) -> Path:
    return reviews_dir(inventory_key) / DECISIONS_FILE


def load_decisions(path: Path) -> list[Decision]:
    if not path.is_file():
        return []
    with open(path, encoding="utf-8") as handle:
        return [Decision.from_dict(json.loads(line)) for line in handle if line.strip()]


def append_decision(path: Path, action: str, targets: Iterable[str], value: str | None = None,
                    now: datetime | None = None) -> Decision:
    targets = tuple(targets)
    validate(action, targets, value)
    ensure_safe_output_dir(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_decisions(path)
    created = now or datetime.now(UTC)
    decision = Decision(stable_id("d", str(len(existing)), action, *targets, value or "", created.isoformat()),
                        action, targets, value, created)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(to_jsonable(decision), ensure_ascii=False) + "\n")
    return decision


def validate(action: str, targets: tuple[str, ...], value: str | None) -> None:
    arity = {"accept": 1, "reject": 1, "rename": 1, "move": 2, "unassign": 1, "label": 1, "unlabel": 1,
             "confirm": 1, "dismiss": 1, "keep_both": 2, "mark_duplicate": 2}
    if action not in GROUP_ACTIONS | RELATIONSHIP_ACTIONS:
        raise DecisionError(f"Unknown action: {action}")
    if action in arity and len(targets) != arity[action]:
        raise DecisionError(f"{action} needs {arity[action]} target(s)")
    if action == "merge" and len(targets) < 2:
        raise DecisionError("merge needs at least two groups")
    if action == "split" and len(targets) < 2:
        raise DecisionError("split needs a group and at least one file")
    if action == "rename" and not (value and value.strip()):
        raise DecisionError("rename needs a new title")
    if action in ("label", "unlabel") and value not in LABELS:
        raise DecisionError(f"label must be one of: {', '.join(sorted(LABELS))}")


class _State:
    def __init__(self, groups: dict[str, GroupBuilder], unassigned: dict[str, str]) -> None:
        self.groups = groups
        self.unassigned = unassigned

    def location(self, file_id: str) -> str | None:
        for g in self.groups.values():
            if file_id in g.members:
                return g.id
        return None

    def take(self, file_id: str) -> list[Evidence] | None:
        """Remove a file from wherever it is; returns its previous evidence."""
        gid = self.location(file_id)
        if gid is not None:
            return self.groups[gid].members.pop(file_id)
        if file_id in self.unassigned:
            del self.unassigned[file_id]
            return []
        return None

    def reparent(self, old: str, new: str | None) -> None:
        for g in self.groups.values():
            if g.parent_id == old:
                g.parent_id = new


def _user(detail: str, decision: Decision) -> Evidence:
    return Evidence("user_decision", 5.0, detail, (decision.id,))


def apply_group_decisions(groups: dict[str, GroupBuilder], unassigned: dict[str, str],
                          decisions: Iterable[Decision]) -> tuple[int, list[str]]:
    state = _State(groups, unassigned)
    applied, stale = 0, []
    for d in decisions:
        if d.action not in GROUP_ACTIONS:
            continue
        ok = _apply_one(state, d)
        applied += ok
        if not ok:
            stale.append(d.id)
    return applied, stale


def _apply_one(state: _State, d: Decision) -> bool:
    groups = state.groups
    t = d.targets
    if d.action in ("accept", "reject", "rename", "label", "unlabel"):
        g = groups.get(t[0])
        if g is None:
            return False
        if d.action == "accept":
            g.status = "accepted"
        elif d.action == "rename":
            g.user_title = (d.value or "").strip()
        elif d.action == "label":
            g.labels = sorted((set(g.labels) - _exclusive(d.value)) | {d.value})  # type: ignore[arg-type]
        elif d.action == "unlabel":
            g.labels = sorted(set(g.labels) - {d.value})
        else:  # reject
            for file_id in list(g.members):
                state.unassigned[file_id] = "its proposed group was rejected by you"
            del groups[g.id]
            state.reparent(g.id, g.parent_id)
        return True
    if d.action == "merge":
        target = groups.get(t[0])
        others = [groups.get(x) for x in t[1:]]
        if target is None or any(o is None for o in others) or target.kind is GroupKind.COURSE_COLLECTION \
                or any(o.kind is GroupKind.COURSE_COLLECTION for o in others):  # type: ignore[union-attr]
            return False
        for other in others:
            for file_id in list(other.members):  # type: ignore[union-attr]
                other.members.pop(file_id)  # type: ignore[union-attr]
                target.members[file_id] = [_user("merged into this group by you", d)]
            del groups[other.id]  # type: ignore[union-attr]
            state.reparent(other.id, target.id)  # type: ignore[union-attr]
        target.evidence.append(_user(f"you merged {len(others)} group(s) into this one", d))
        return True
    if d.action == "split":
        source = groups.get(t[0])
        if source is None or source.kind is GroupKind.COURSE_COLLECTION:
            return False
        moving = [f for f in t[1:] if f in source.members]
        if not moving:
            return False
        new = GroupBuilder(
            id=stable_id("g", "split", d.id), kind=source.kind if source.kind is not GroupKind.LOOSE_COLLECTION
            else GroupKind.PROJECT, title=(d.value or f"{source.title} (split)"), anchor=source.anchor,
            parent_id=source.parent_id, evidence=[_user("split out by you", d)], course_hints=source.course_hints,
        )
        for file_id in moving:
            source.members.pop(file_id)
            new.members[file_id] = [_user("split out by you", d)]
        groups[new.id] = new
        return True
    if d.action == "move":
        file_id, gid = t
        target = groups.get(gid)
        if target is None or target.kind is GroupKind.COURSE_COLLECTION:
            return False
        if state.take(file_id) is None:
            return False
        target.members[file_id] = [_user("moved into this group by you", d)]
        return True
    if d.action == "unassign":
        if state.take(t[0]) is None:
            return False
        state.unassigned[t[0]] = "unassigned by you"
        return True
    return False


def _exclusive(label: str | None) -> set[str]:
    return {"mine", "third_party"} - {label} if label in ("mine", "third_party") else set()


def apply_relationship_decisions(relationships: list[Relationship], groups: Iterable[CandidateGroup],
                                 decisions: Iterable[Decision]) -> tuple[list[Relationship], int, list[str]]:
    by_id = {r.id: r for r in relationships}
    group_ids = {g.id for g in groups}
    applied, stale = 0, []
    for d in decisions:
        if d.action not in RELATIONSHIP_ACTIONS:
            continue
        if d.action in ("confirm", "dismiss"):
            rel = by_id.get(d.targets[0])
            if rel is None:
                stale.append(d.id)
                continue
            by_id[rel.id] = replace(rel, status="confirmed" if d.action == "confirm" else "dismissed")
        else:
            a, b = d.targets
            if a not in group_ids or b not in group_ids:
                stale.append(d.id)
                continue
            pair = {a, b}
            matching = [r for r in by_id.values() if {r.source, r.target} == pair
                        and r.kind in (RelationKind.POSSIBLE_DUPLICATE, RelationKind.POSSIBLE_VARIANT)]
            if d.action == "keep_both":
                if not matching:
                    stale.append(d.id)
                    continue
                for r in matching:
                    by_id[r.id] = replace(r, status="dismissed")
            else:  # mark_duplicate
                if matching:
                    for r in matching:
                        by_id[r.id] = replace(r, status="confirmed")
                else:
                    first, second = sorted(pair)
                    rid = stable_id("r", "user_duplicate", first, second)
                    by_id[rid] = Relationship(rid, a, b, RelationKind.POSSIBLE_DUPLICATE, 5.0,
                                              (_user("marked as duplicates by you", d),), status="confirmed",
                                              likely_original=b)
        applied += 1
    return sorted(by_id.values(), key=lambda r: r.id), applied, stale


