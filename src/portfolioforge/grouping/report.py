"""Readable views of a grouping run: terminal listings and a local Markdown report.

With ``redact``, no paths, folder names or file names are shown: groups appear
by id, kind, counts and confidence only.
"""

from __future__ import annotations

from collections import defaultdict

from ..inventory.models import Inventory
from .models import CandidateGroup, GroupKind, GroupingRun

PRIVACY_NOTE = (
    "This report lists the names and locations of your personal files. Keep it on this computer: "
    "do not paste it into chats, issues or AI tools, and avoid opening it in an editor with AI extensions enabled."
)


def _title(g: CandidateGroup, redact: bool) -> str:
    if redact:
        return f"{g.kind.value} {g.id}"
    title = g.user_title or g.working_title.text
    return f"{title}  [{g.id}]"


def _labels(g: CandidateGroup) -> str:
    bits = [g.status.value] if g.status.value != "proposed" else []
    bits += list(g.labels)
    return f" ({', '.join(bits)})" if bits else ""


def format_list(run: GroupingRun, *, redact: bool = False, flagged_only: bool = False,
                band: str | None = None, kind: str | None = None) -> str:
    flags_by_target: dict[str, list[str]] = defaultdict(list)
    for f in run.flags:
        flags_by_target[f.target].append(f.kind.value)
    children: dict[str | None, list[CandidateGroup]] = defaultdict(list)
    for g in run.groups:
        children[g.parent_id].append(g)

    def visible(g: CandidateGroup) -> bool:
        if flagged_only and not flags_by_target.get(g.id):
            return False
        if band and (g.kind is GroupKind.COURSE_COLLECTION or g.band.value != band):
            return False
        return not (kind and g.kind.value != kind)

    lines: list[str] = []

    def emit(g: CandidateGroup, depth: int) -> None:
        shown = visible(g)
        if shown:
            flags = f"  flags: {', '.join(sorted(set(flags_by_target[g.id])))}" if flags_by_target.get(g.id) else ""
            size = f"{len(g.members)} files" if g.kind is not GroupKind.COURSE_COLLECTION \
                else f"{g.summary.get('child_groups', 0)} groups"
            conf = f"{g.band.value} {g.confidence:.2f}"
            lines.append(f"{'  ' * depth}- {_title(g, redact)}  {g.kind.value}, {size}, {conf}{_labels(g)}{flags}")
        for c in children.get(g.id, []):
            emit(c, depth + 1 if shown else depth)

    for top in children.get(None, []):
        emit(top, 0)
    s = run.stats
    header = (f"{len([g for g in run.groups if g.kind is not GroupKind.COURSE_COLLECTION])} groups, "
              f"{s['grouped_files']:,} of {s['files']:,} files grouped, {s['unassigned_files']:,} unassigned, "
              f"{len(run.relationships)} suggested relationships, {len(run.flags)} review flags")
    if run.stale_decisions:
        header += f", {len(run.stale_decisions)} out-of-date decisions"
    return "\n".join([header, "", *(lines or ["(no groups match)"])])


def format_group(run: GroupingRun, group_id: str, inventory: Inventory, *, redact: bool = False) -> str:
    g = run.group(group_id)
    if g is None:
        raise KeyError(group_id)
    files = {f.id: f for f in inventory.files}
    lines = [
        _title(g, redact) + _labels(g),
        f"  Kind: {g.kind.value}    Confidence: {g.band.value} ({g.confidence:.2f})",
        "  Working title is provisional; final repository names come from a later, approved naming stage.",
    ]
    if g.course_hints and not redact:
        lines.append(f"  Course hints: {', '.join(g.course_hints)}")
    if g.summary.get("location") and not redact:
        lines.append(f"  Location: {g.summary['location']}")
    lines.append("  Why these files belong together:")
    lines += [f"    {e.weight:+.1f}  {e.detail if not redact else e.kind}" for e in g.evidence]
    if g.members:
        lines.append(f"  Files ({len(g.members)}):")
        for m in g.members:
            name = m.file_id if redact else files[m.file_id].relative_path if m.file_id in files else m.file_id
            reasons = "; ".join(e.detail if not redact else e.kind for e in m.evidence)
            lines.append(f"    {name}  [{m.role}, {m.score:.2f}]  {reasons}")
    flags = [f for f in run.flags if f.target == g.id]
    if flags:
        lines.append("  Review flags:")
        for f in flags:
            detail = "; ".join(e.detail if not redact else e.kind for e in f.evidence)
            lines.append(f"    {f.kind.value} ({f.severity}): {detail}")
    rels = [r for r in run.relationships if g.id in (r.source, r.target)]
    if rels:
        lines.append("  Suggested relationships (never applied automatically):")
        for r in rels:
            other = r.target if r.source == g.id else r.source
            other_group = run.group(other)
            label = _title(other_group, redact) if other_group else other
            merge = "  [merge suggested]" if r.suggest_merge else ""
            lines.append(f"    {r.kind.value} {label}  score {r.score:.1f}  {r.status}{merge}  [{r.id}]")
    return "\n".join(lines)


def markdown_report(run: GroupingRun, inventory: Inventory) -> str:
    files = {f.id: f for f in inventory.files}
    out = ["# PortfolioForge candidate groups", "", f"> {PRIVACY_NOTE}", "",
           "Groups are **proposals** from file and folder metadata only. Working titles are provisional, "
           "and nothing is merged, moved or published without your decision.", "",
           "## Summary", ""]
    out += [f"- {k.replace('_', ' ')}: {v}" for k, v in run.stats.items()]
    if run.stale_decisions:
        out.append(f"- out-of-date decisions: {len(run.stale_decisions)}")
    out += ["", "## Groups", ""]
    children: dict[str | None, list[CandidateGroup]] = defaultdict(list)
    for g in run.groups:
        children[g.parent_id].append(g)
    flags_by_target: dict[str, list[str]] = defaultdict(list)
    for f in run.flags:
        flags_by_target[f.target].append(f"{f.kind.value}: " + "; ".join(e.detail for e in f.evidence))

    def emit(g: CandidateGroup, depth: int) -> None:
        indent = "  " * depth
        title = g.user_title or g.working_title.text
        out.append(f"{indent}- **{title}** `{g.id}`: {g.kind.value}, {g.band.value} confidence ({g.confidence:.2f})"
                   + (f", {', '.join(g.labels)}" if g.labels else ""))
        for e in g.evidence:
            out.append(f"{indent}  - evidence: {e.detail} ({e.weight:+.1f})")
        for flag in flags_by_target.get(g.id, []):
            out.append(f"{indent}  - flag: {flag}")
        for m in g.members:
            record = files.get(m.file_id)
            out.append(f"{indent}  - `{record.relative_path if record else m.file_id}` ({m.role})")
        for c in children.get(g.id, []):
            emit(c, depth + 1)

    for top in children.get(None, []):
        emit(top, 0)
    if run.relationships:
        out += ["", "## Suggested relationships", "", "Suggestions only; use `merge`, `confirm` or `dismiss` to act.", ""]
        for r in run.relationships:
            out.append(f"- `{r.id}` {r.kind.value} between `{r.source}` and `{r.target}` "
                       f"(score {r.score:.1f}, {r.status}{', merge suggested' if r.suggest_merge else ''}): "
                       + "; ".join(e.detail for e in r.evidence))
    if run.unassigned:
        out += ["", "## Unassigned files", ""]
        for u in run.unassigned:
            record = files.get(u.file_id)
            near = f"; nearest: {', '.join(g for g, _ in u.nearest)}" if u.nearest else ""
            out.append(f"- `{record.relative_path if record else u.file_id}` `{u.file_id}`: {u.reason}{near}")
    return "\n".join(out) + "\n"
