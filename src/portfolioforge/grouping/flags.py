"""Review flags. A flag asks the user to look; it never decides anything.

In particular, ``possible_third_party`` never asserts who wrote the code:
it lists the signals (version control plus community files, bulk-copied
timestamps, a name the user listed as third-party) and leaves the call to
the user's review decision.
"""

from __future__ import annotations

from typing import Iterable

from ..inventory.models import FileRecord
from .clustering import uniform_timestamps
from .config import GroupingConfig
from .features import assignment_marker, is_generic_name, tokenize
from .index import InventoryIndex
from .models import Band, CandidateGroup, Evidence, FlagKind, GroupKind, RelationKind, Relationship, ReviewFlag
from .scoring import stable_id

_COMMUNITY_FILES = ("license", "copying", "contributing", "code_of_conduct", "changelog", "changes")


def compute_flags(groups: Iterable[CandidateGroup], relationships: Iterable[Relationship],
                  index: InventoryIndex, config: GroupingConfig) -> list[ReviewFlag]:
    flags: list[ReviewFlag] = []

    def add(target: str, kind: FlagKind, severity: str, evidence: list[Evidence]) -> None:
        flags.append(ReviewFlag(stable_id("f", kind.value, target), target, kind, severity, tuple(evidence)))

    for g in groups:
        if g.kind is GroupKind.COURSE_COLLECTION or not g.members:
            continue
        records = [index.files[m.file_id] for m in g.members]
        times = [r.modified_at.timestamp() for r in records if r.modified_at]
        uniform = uniform_timestamps(times, config)
        title_tokens = set(tokenize(g.working_title.text))
        anchor = index.folders[g.anchor] if g.anchor and g.summary.get("folder_unit") else None

        third: list[Evidence] = []
        if anchor and anchor.marker_dirs & {".git", ".github", ".gitlab"}:
            third.append(Evidence("third_party_vcs", config.w("third_party_vcs"),
                                  "has its own version-control history (.git)", (g.id,)))
        if anchor and any(m.startswith(_COMMUNITY_FILES) for m in anchor.marker_files):
            third.append(Evidence("third_party_community_files", config.w("third_party_community_files"),
                                  "has LICENSE / CONTRIBUTING / CHANGELOG files typical of published projects", (g.id,)))
        if uniform:
            third.append(Evidence("third_party_uniform_times", config.w("third_party_uniform_times"),
                                  "almost all files have the same modification time (cloned or extracted at once)",
                                  (g.id,)))
        known = {n for n in config.known_third_party_names
                 if n == g.working_title.text.casefold() or n in title_tokens}
        if known:
            third.append(Evidence("third_party_known_name", config.w("third_party_known_name"),
                                  f"matches a name in your third-party list ({', '.join(sorted(known))})", (g.id,)))
        if sum(e.weight for e in third) >= config.t("third_party_flag"):
            add(g.id, FlagKind.POSSIBLE_THIRD_PARTY, "review", third)

        template: list[Evidence] = []
        if title_tokens & config.template_words:
            template.append(Evidence("template_name", config.w("template_name"),
                                     "name suggests a template or starter code", (g.id,)))
        if uniform and template:
            template.append(Evidence("template_uniform_times", config.w("template_uniform_times"),
                                     "files were all copied at the same time", (g.id,)))
        if sum(e.weight for e in template) >= config.t("template_flag"):
            add(g.id, FlagKind.POSSIBLE_TEMPLATE, "review", template)

        if all(is_generic_name(r.stem, config) for r in records):
            add(g.id, FlagKind.GENERIC_NAMES, "info",
                [Evidence("generic_names", 0.0, "every file has a generic name (e.g. 'Untitled', 'main')", (g.id,))])

        data_bytes = sum(r.size_bytes for r in records if r.category.value in ("data", "spreadsheet", "archive"))
        if data_bytes >= config.large_dataset_bytes:
            add(g.id, FlagKind.LARGE_DATASET, "review",
                [Evidence("large_dataset", 0.0,
                          f"contains {data_bytes / 1_048_576:,.0f} MB of data files; review before any analysis "
                          "or publishing", (g.id,))])

        if anchor:
            direct = [r for r in records if r.parent_dir == anchor.rel]
            keys = _numbered_keys(direct, config)
            if len(keys) >= 2:
                add(g.id, FlagKind.MIXED_CONTENT, "review",
                    [Evidence("mixed_assignments", 0.0,
                              f"files are named for {len(keys)} different assignments ({', '.join(sorted(keys))}); "
                              "consider splitting", (g.id,))])

        if g.band is Band.LOW:
            add(g.id, FlagKind.SPARSE_EVIDENCE, "info",
                [Evidence("sparse_evidence", 0.0, "little evidence that these files belong together", (g.id,))])

    for rel in relationships:
        if rel.kind is RelationKind.POSSIBLE_DUPLICATE and rel.status != "dismissed" \
                and not rel.source.startswith("file:"):
            for target in (rel.source, rel.target):
                add(target, FlagKind.POSSIBLE_DUPLICATE, "review",
                    [Evidence("duplicate_relationship", 0.0, "may be a copy of another group; both are kept",
                              (rel.id,))])
    unique: dict[str, ReviewFlag] = {}
    for flag in flags:
        unique.setdefault(flag.id, flag)
    return sorted(unique.values(), key=lambda f: f.id)


def _numbered_keys(records: list[FileRecord], config: GroupingConfig) -> set[str]:
    keys = set()
    for r in records:
        marker = assignment_marker(r.stem, config)
        if marker and marker.number:
            keys.add(marker.key)
    return keys
