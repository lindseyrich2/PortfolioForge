"""Grouping output: candidate groups, relationships, review flags, decisions.

Everything here is derived from inventory *metadata* only. Groups refer to
files by inventory file id. All models round-trip through JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import Enum, StrEnum
from typing import Any

GROUPING_SCHEMA_VERSION = 1


class GroupKind(StrEnum):
    COURSE_COLLECTION = "course_collection"
    """A course or term folder; holds assignment/project groups, no files of its own."""
    PROJECT = "project"
    ASSIGNMENT = "assignment"
    LOOSE_COLLECTION = "loose_collection"
    """Files in one folder with no stronger grouping evidence."""


class GroupStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"


class Band(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RelationKind(StrEnum):
    RELATED_TO = "related_to"
    REPORT_FOR = "report_for"
    SAME_COURSE = "same_course"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    POSSIBLE_VARIANT = "possible_variant"


class FlagKind(StrEnum):
    POSSIBLE_DUPLICATE = "possible_duplicate"
    POSSIBLE_THIRD_PARTY = "possible_third_party"
    POSSIBLE_TEMPLATE = "possible_template"
    GENERIC_NAMES = "generic_names"
    MIXED_CONTENT = "mixed_content"
    LARGE_DATASET = "large_dataset"
    SPARSE_EVIDENCE = "sparse_evidence"


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: str
    """Machine-readable kind, e.g. ``assignment_number`` or ``project_marker``."""
    weight: float
    """Signed contribution to the score."""
    detail: str
    """Human-readable explanation."""
    refs: tuple[str, ...] = ()
    """Ids of the files, folders or groups this evidence is about."""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Evidence:
        return cls(d["kind"], d["weight"], d["detail"], tuple(d.get("refs", ())))


@dataclass(frozen=True, slots=True)
class Membership:
    file_id: str
    role: str
    """code | notebook | report | slides | data | config | other"""
    score: float
    evidence: tuple[Evidence, ...]
    source: str = "local_inventory"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Membership:
        return cls(d["file_id"], d["role"], d["score"],
                   tuple(Evidence.from_dict(e) for e in d["evidence"]), d.get("source", "local_inventory"))


@dataclass(frozen=True, slots=True)
class ReviewFlag:
    id: str
    target: str
    """A group id or a file id."""
    kind: FlagKind
    severity: str
    """info | review"""
    evidence: tuple[Evidence, ...]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReviewFlag:
        return cls(d["id"], d["target"], FlagKind(d["kind"]), d["severity"],
                   tuple(Evidence.from_dict(e) for e in d["evidence"]))


@dataclass(frozen=True, slots=True)
class WorkingTitle:
    """A provisional label from folder and file names. Never a final repository name."""

    text: str
    provisional: bool = True
    source: str = "names"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WorkingTitle:
        return cls(d["text"], d.get("provisional", True), d.get("source", "names"))


@dataclass(frozen=True, slots=True)
class CandidateGroup:
    id: str
    """Stable across reruns: derived from the anchor folder (and cluster seed)."""
    kind: GroupKind
    working_title: WorkingTitle
    anchor: str | None
    """Folder id (``<root id>:<relative path>``) the group is anchored to."""
    parent_id: str | None
    members: tuple[Membership, ...]
    confidence: float
    band: Band
    evidence: tuple[Evidence, ...]
    flag_ids: tuple[str, ...] = ()
    course_hints: tuple[str, ...] = ()
    time_span: tuple[datetime | None, datetime | None] = (None, None)
    summary: dict[str, Any] = field(default_factory=dict)
    status: GroupStatus = GroupStatus.PROPOSED
    user_title: str | None = None
    labels: tuple[str, ...] = ()
    """User-set labels: ``mine``, ``third_party``, ``ignored``. Never set automatically."""

    @property
    def file_ids(self) -> tuple[str, ...]:
        return tuple(m.file_id for m in self.members)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CandidateGroup:
        span = d.get("time_span") or (None, None)
        return cls(
            id=d["id"], kind=GroupKind(d["kind"]), working_title=WorkingTitle.from_dict(d["working_title"]),
            anchor=d["anchor"], parent_id=d["parent_id"],
            members=tuple(Membership.from_dict(m) for m in d["members"]),
            confidence=d["confidence"], band=Band(d["band"]),
            evidence=tuple(Evidence.from_dict(e) for e in d["evidence"]),
            flag_ids=tuple(d.get("flag_ids", ())), course_hints=tuple(d.get("course_hints", ())),
            time_span=tuple(datetime.fromisoformat(t) if t else None for t in span),  # type: ignore[arg-type]
            summary=dict(d.get("summary", {})), status=GroupStatus(d.get("status", "proposed")),
            user_title=d.get("user_title"), labels=tuple(d.get("labels", ())),
        )


@dataclass(frozen=True, slots=True)
class Relationship:
    id: str
    source: str
    target: str
    """Group ids, or ``file:<id>`` for file-level relationships."""
    kind: RelationKind
    score: float
    evidence: tuple[Evidence, ...]
    suggest_merge: bool = False
    """A merge suggestion for the user. Groups are never merged automatically."""
    status: str = "suggested"
    """suggested | confirmed | dismissed (set only by user decisions)"""
    likely_original: str | None = None
    """For duplicates: which side looks like the original. A suggestion only."""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Relationship:
        return cls(d["id"], d["source"], d["target"], RelationKind(d["kind"]), d["score"],
                   tuple(Evidence.from_dict(e) for e in d["evidence"]), d.get("suggest_merge", False),
                   d.get("status", "suggested"), d.get("likely_original"))


@dataclass(frozen=True, slots=True)
class UnassignedFile:
    file_id: str
    reason: str
    nearest: tuple[tuple[str, float], ...] = ()
    """(group id, score) suggestions for review."""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UnassignedFile:
        return cls(d["file_id"], d["reason"], tuple((g, s) for g, s in d.get("nearest", ())))


@dataclass(frozen=True, slots=True)
class Decision:
    """One user correction, stored in the local review ledger."""

    id: str
    action: str
    targets: tuple[str, ...]
    value: str | None
    created_at: datetime

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Decision:
        return cls(d["id"], d["action"], tuple(d["targets"]), d.get("value"),
                   datetime.fromisoformat(d["created_at"]))


@dataclass(frozen=True, slots=True)
class GroupingRun:
    schema_version: int
    inventory_ref: dict[str, Any]
    """Identifies the source inventory: key, schema version, scan start time, report digest."""
    config_digest: str
    created_at: datetime
    groups: tuple[CandidateGroup, ...]
    relationships: tuple[Relationship, ...]
    flags: tuple[ReviewFlag, ...]
    unassigned: tuple[UnassignedFile, ...]
    decisions_applied: int
    stale_decisions: tuple[str, ...]
    """Ids of decisions that refer to groups or files no longer present."""
    stats: dict[str, Any]

    def group(self, group_id: str) -> CandidateGroup | None:
        return next((g for g in self.groups if g.id == group_id), None)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> GroupingRun:
        if d.get("schema_version") != GROUPING_SCHEMA_VERSION:
            raise ValueError(f"Unsupported grouping schema version: {d.get('schema_version')!r}")
        return cls(
            schema_version=d["schema_version"], inventory_ref=dict(d["inventory_ref"]),
            config_digest=d["config_digest"], created_at=datetime.fromisoformat(d["created_at"]),
            groups=tuple(CandidateGroup.from_dict(g) for g in d["groups"]),
            relationships=tuple(Relationship.from_dict(r) for r in d["relationships"]),
            flags=tuple(ReviewFlag.from_dict(f) for f in d["flags"]),
            unassigned=tuple(UnassignedFile.from_dict(u) for u in d["unassigned"]),
            decisions_applied=d["decisions_applied"], stale_decisions=tuple(d["stale_decisions"]),
            stats=dict(d["stats"]),
        )


def to_jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, float):
        return round(value, 4)
    return value
