"""Conservative, metadata-only eligibility filter. Reads no files.

Decides which groups *may be offered* for analysis. It never approves
anything: even a ``technical_candidate`` needs the user's explicit file
approval, and uncertain groups (``needs_review``) are never selected
automatically. It also never decides ownership or publishability.
"""

from __future__ import annotations

from typing import Iterable

from ..grouping.features import tokenize
from ..grouping.models import Band, CandidateGroup, FlagKind, GroupingRun, GroupKind
from ..inventory.models import FileRecord, Inventory
from .models import Eligibility, EligibilityStatus

#: Name words typical of personal or administrative documents (generic, configurable later).
#: Kept narrow on purpose: words such as "license", "application" or "statement" are
#: common in coding projects (LICENSE files, JavaApplication1, problem statements).
ADMIN_WORDS: frozenset[str] = frozenset({
    "resume", "résumé", "cv", "transcript", "transcripts", "invoice", "receipt", "receipts", "tax", "taxes",
    "w2", "w-2", "lease", "insurance", "medical", "prescription", "passport", "visa", "payslip", "paystub",
    "salary", "fafsa", "scholarship", "ssn", "immigration", "wedding", "diary", "bank",
})

#: Flags that make a group uncertain.
REVIEW_FLAGS: frozenset[FlagKind] = frozenset({
    FlagKind.POSSIBLE_THIRD_PARTY, FlagKind.POSSIBLE_TEMPLATE, FlagKind.MIXED_CONTENT,
    FlagKind.POSSIBLE_DUPLICATE, FlagKind.GENERIC_NAMES,
})

_CODE = frozenset({"code"})
_NOTEBOOK = frozenset({"notebook"})
_DOCS = frozenset({"document", "presentation", "spreadsheet"})
MIN_CODE_SHARE = 0.3


def assess_group(group: CandidateGroup, files: dict[str, FileRecord], flag_kinds: Iterable[FlagKind]) -> Eligibility:
    records = [files[m.file_id] for m in group.members if m.file_id in files]
    cats = [r.category.value for r in records]
    code = sum(c in _CODE for c in cats)
    notebooks = sum(c in _NOTEBOOK for c in cats)
    docs = sum(c in _DOCS for c in cats)
    total = len(records)

    def result(status: EligibilityStatus, *reasons: str) -> Eligibility:
        return Eligibility(group.id, status, tuple(reasons), code, notebooks, docs, total)

    if group.kind is GroupKind.COURSE_COLLECTION:
        return result(EligibilityStatus.EXCLUDED, "course collections are analyzed through their assignments and projects")
    if "ignored" in group.labels:
        return result(EligibilityStatus.EXCLUDED, "you labelled this group 'ignored'")
    if "third_party" in group.labels:
        return result(EligibilityStatus.EXCLUDED, "you labelled this group 'third_party'")

    words = set(tokenize(group.user_title or group.working_title.text))
    for r in records:
        words |= set(tokenize(r.stem))
    admin = sorted(words & ADMIN_WORDS)
    code_like = code + notebooks

    if code_like == 0:
        # Only documents whose names look like personal paperwork are personal/admin.
        # Other code-free groups (a project report, slides, data) stay reviewable.
        if admin:
            return result(EligibilityStatus.PERSONAL_OR_ADMIN, "contains no code or notebooks",
                          f"names suggest personal or administrative documents ({', '.join(admin)})")
        return result(EligibilityStatus.NEEDS_REVIEW, "contains no code or notebooks; review manually")

    reasons: list[str] = []
    if admin:
        reasons.append(f"some names suggest personal or administrative documents ({', '.join(admin)})")
    if code_like / max(total, 1) < MIN_CODE_SHARE:
        reasons.append(f"code and notebooks are only {code_like} of {total} files")
    if group.kind is GroupKind.LOOSE_COLLECTION:
        reasons.append("loose files grouped only by folder")
    if group.band is Band.LOW:
        reasons.append("low grouping confidence")
    flagged = sorted({f.value for f in flag_kinds} & {f.value for f in REVIEW_FLAGS})
    if flagged:
        reasons.append(f"review flags: {', '.join(flagged)}")
    if reasons:
        return result(EligibilityStatus.NEEDS_REVIEW, *reasons)
    return result(EligibilityStatus.TECHNICAL_CANDIDATE,
                  f"{code_like} code/notebook files of {total}", f"{group.band.value} grouping confidence")


def assess_run(run: GroupingRun, inventory: Inventory) -> tuple[Eligibility, ...]:
    files = {f.id: f for f in inventory.files}
    flags: dict[str, list[FlagKind]] = {}
    for flag in run.flags:
        flags.setdefault(flag.target, []).append(flag.kind)
    return tuple(assess_group(g, files, flags.get(g.id, ())) for g in run.groups)
