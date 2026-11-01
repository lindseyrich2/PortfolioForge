"""Metadata-only project identification and candidate grouping.

Consumes a saved inventory (schema v1 or v2) and proposes which files belong
together, as a course → assignment/project hierarchy with explainable
evidence and confidence scores. Cross-folder relationships, duplicates and
possible third-party code are *suggestions for review*; nothing is merged,
discarded or attributed automatically. User corrections are stored locally and
re-applied on every run.

This stage never opens original files, never calls an AI model or any other
service, and keeps all results in the user's local data folder.
"""

from .config import GroupingConfig, GroupingConfigError, config_from_dict, load_grouping_config
from .engine import GroupingInvariantError, propose_groups
from .models import (
    Band,
    CandidateGroup,
    Decision,
    Evidence,
    FlagKind,
    GroupKind,
    GroupingRun,
    GroupStatus,
    Membership,
    RelationKind,
    Relationship,
    ReviewFlag,
    UnassignedFile,
    WorkingTitle,
)
from .review import DecisionError, append_decision, decisions_path, load_decisions

__all__ = [
    "Band", "CandidateGroup", "Decision", "DecisionError", "Evidence", "FlagKind", "GroupKind",
    "GroupingConfig", "GroupingConfigError", "GroupingInvariantError", "GroupingRun", "GroupStatus",
    "Membership", "RelationKind", "Relationship", "ReviewFlag", "UnassignedFile", "WorkingTitle",
    "append_decision", "config_from_dict", "decisions_path", "load_decisions", "load_grouping_config",
    "propose_groups",
]
