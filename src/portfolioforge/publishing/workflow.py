"""Publishing one accepted project to its own new GitHub repository. Language-agnostic.

    stage     copy explicitly selected files through the guarded reader into a staging folder
              outside any repository, and generate README.md and PROVENANCE.md
    checks    privacy, sensitive data, ownership, attribution and licensing, on the staged bytes
    preview   exactly what would be published
    approve   typed, single-use, and bound to the staged files, name, visibility and account
    publish   everything is checked again, then the GitHub CLI creates a NEW repository

High-risk findings block, and that includes anything that could identify a person and anything
that could not be scanned. Actual secrets can only be fixed; other high-risk findings can be
resolved one at a time with a recorded reason. An existing repository is never touched: a name
collision, or any answer other than a clear "not found", stops the run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Sequence

from ..devtools.privacy_check import check_file
from ..grouping.models import CandidateGroup
from ..grouping.scoring import stable_id
from ..inventory import rules
from ..inventory.models import Inventory
from ..paths import ensure_safe_output_dir
from ..privacy import PrivacySettings
from ..understanding.analysis import AnalysisProposal
from ..understanding.models import ApprovedFile, Eligibility, FileApprovalRecord, dumps
from ..understanding.reader import ApprovedFileReader, ReadRefused
from ..understanding.redact import redact
from ..understanding.selection import exclusion_reason, project_path, require_approvable
from ..understanding.sensitive import scan_text
from ..understanding.store import _write, group_dir, latest_build_approval

PRIVATE, PUBLIC = "private", "public"
VISIBILITIES = (PRIVATE, PUBLIC)
OWNERSHIP = ("mine", "team", "includes-starter-code", "includes-third-party")
COURSE_POLICY = ("not-coursework", "publishing-permitted")
GENERATED = ("README.md", "PROVENANCE.md")
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_SAFE_LICENSE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,39}$")
_LICENSE_FILE = re.compile(r"(?i)^(?:license|licence|copying)(?:\.\w+)?$")
_THIRD_PARTY_NOTICE = re.compile(
    r"(?i)\bcopyright\s+(?:\(c\)|©|\d{4})|\bSPDX-License-Identifier\b|\blicensed\s+under\b|\bGNU\s+General\s+Public\b"
)
_NOTEBOOK_OUTPUTS = re.compile(r'"outputs"\s*:\s*\[\s*\{')
_THIRD_PARTY_PATH = re.compile(r"(?i)(?:^|/)(?:lib|libs|vendor|third[_-]?party|node_modules)/|\.(?:jar|class|dll|so|dylib|exe)$")
#: Real secrets and pipeline records. These can only be fixed, never accepted.
_NEVER_RESOLVABLE = frozenset({"private_key", "github_token", "aws_access_key", "personal_data_file", "inventory_data",
                               "sensitive_file", "generated_file_leak", "unconfirmed_ownership",
                               "unconfirmed_course_policy", "license_file_missing", "license_not_chosen",
                               "attribution_missing"})
_PRIVACY_RULES_HIGH = frozenset({"personal_data_file", "inventory_data", "private_key", "github_token", "aws_access_key",
                                 "generic_secret", "absolute_home_path", "local_identity", "data_file", "large_file"})

Runner = Callable[[Sequence[str], str | None], "ToolResult"]


class PublishRefused(PermissionError):
    """Stops the workflow. Messages never include local paths."""


@dataclass(frozen=True)
class ToolResult:
    code: int
    stdout: str
    stderr: str


def run_tool(argv: Sequence[str], cwd: str | None) -> ToolResult:
    """Run git or the GitHub CLI, without a shell or input."""
    # An inherited GIT_DIR would point git at some other repository.
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    try:
        done = subprocess.run(list(argv), cwd=cwd, shell=False, stdin=subprocess.DEVNULL, capture_output=True, env=env,
                              text=True, encoding="utf-8", errors="replace", timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ToolResult(127, "", type(exc).__name__)
    return ToolResult(done.returncode, done.stdout, done.stderr)


@dataclass(frozen=True)
class Candidate:
    file_id: str
    project_path: str
    absolute_path: str
    size_bytes: int
    modified_at: str | None
    category: str


@dataclass(frozen=True)
class StagedFile:
    path: str
    sha256: str
    size_bytes: int
    git_blob: str
    """Git's id for the approved bytes, so the commit itself can be checked against them."""
    generated: bool = False


@dataclass(frozen=True)
class Stage:
    id: str
    inventory_key: str
    group_id: str
    proposal_id: str
    repo_name: str
    visibility: str
    ownership: str | None
    course_policy: str | None
    license: str | None
    attribution: str | None
    attribution_concerns: tuple[str, ...]
    files: tuple[StagedFile, ...]
    digest: str
    created_at: str
    resolutions: dict[str, str] = field(default_factory=dict)
    """Finding id -> your reason for accepting it. Local only; never published."""


@dataclass(frozen=True)
class Finding:
    id: str
    check: str
    """privacy | sensitive | ownership | attribution | licensing"""
    kind: str
    severity: str
    """high (blocks until fixed or resolved) | review (shown; covered by your approval)"""
    path: str | None
    line: int | None
    detail: str
    resolved: str | None = None

    @property
    def blocks(self) -> bool:
        return self.severity == "high" and self.resolved is None

    @property
    def resolvable(self) -> bool:
        return self.kind not in _NEVER_RESOLVABLE


@dataclass(frozen=True)
class Approval:
    id: str
    stage_id: str
    digest: str
    repo_name: str
    visibility: str
    owner: str
    approved_at: str
    used_at: str | None = None


# --- selecting and staging -------------------------------------------------------------------------


def list_candidates(group: CandidateGroup, inventory: Inventory, privacy: PrivacySettings,
                    inventory_key: str) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """What may be selected, and what never may be. Metadata only. Nothing is selected by default."""
    records = {f.id: f for f in inventory.files}
    allowed: dict[str, Candidate] = {}
    refused: list[tuple[str, str]] = []
    for member in group.members:
        record = records.get(member.file_id)
        if record is None:
            continue
        path = project_path(record, group, inventory)
        reason = exclusion_reason(record, privacy)
        if reason:
            refused.append((rules.REDACTED if reason == "sensitive file name" else path, reason))
            continue
        allowed[path] = Candidate(record.id, path, record.absolute_path, record.size_bytes,
                                  record.modified_at.isoformat() if record.modified_at else None, record.category.value)
    build_approval = latest_build_approval(inventory_key, group.id)
    for approved in (build_approval.files if build_approval else ()):
        allowed.setdefault(approved.project_path, Candidate(
            approved.file_id, approved.project_path, approved.absolute_path, approved.size_bytes, approved.modified_at,
            "build configuration"))
    return sorted(allowed.values(), key=lambda c: c.project_path), refused


def _safe_stage_path(path: str) -> bool:
    pure = PurePosixPath(path)
    return bool(path) and not pure.is_absolute() and ".." not in pure.parts \
        and ".git" not in {part.casefold() for part in pure.parts} \
        and not path.startswith("-") and ":" not in path and "\\" not in path


def stage_dir(inventory_key: str, group_id: str, stage_id: str) -> Path:
    return group_dir(inventory_key, group_id) / "staging" / stage_id


def _git_blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _digest(repo_name: str, visibility: str, ownership: str | None, course_policy: str | None, license_id: str | None,
            attribution: str | None, concerns: Iterable[str], files: Iterable[StagedFile]) -> str:
    body = {"name": repo_name, "visibility": visibility, "ownership": ownership, "course_policy": course_policy,
            "license": license_id, "attribution": attribution, "attribution_concerns": list(concerns),
            "files": sorted([f.path, f.sha256, f.git_blob, f.generated] for f in files)}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def create_stage(*, inventory_key: str, group: CandidateGroup, eligibility: Eligibility, proposal: AnalysisProposal,
                 inventory: Inventory, privacy: PrivacySettings, file_ids: list[str], visibility: str = PRIVATE,
                 ownership: str | None = None, course_policy: str | None = None, license_id: str | None = None,
                 attribution: str | None = None, now: datetime | None = None) -> Stage:
    """Copy the selected files, unchanged, and generate README.md and PROVENANCE.md."""
    if proposal.status != "accepted" or not proposal.accepted_name:
        raise PublishRefused("Accept the proposal and its repository name first (accept-proposal).")
    if proposal.group_id != group.id:
        raise PublishRefused("This proposal belongs to a different project.")
    require_approvable(group, eligibility, True)  # accepting the proposal already covered the review
    if not _SAFE_NAME.match(proposal.accepted_name) or proposal.accepted_name.endswith((".", ".git")):
        raise PublishRefused("The accepted repository name is not valid on GitHub.")
    if visibility not in VISIBILITIES:
        raise PublishRefused("Visibility must be private or public.")
    if license_id and license_id != "none" and not _SAFE_LICENSE.match(license_id):
        raise PublishRefused("The license must be an SPDX identifier such as MIT, or 'none'.")
    if not file_ids:
        raise PublishRefused("Select the files to publish explicitly. Nothing is included by default.")

    candidates, _ = list_candidates(group, inventory, privacy, inventory_key)
    by_id = {c.file_id: c for c in candidates}
    unknown = [f for f in file_ids if f not in by_id]
    if unknown:
        raise PublishRefused(f"{len(unknown)} selected file(s) are not selectable for this project.")
    chosen = [by_id[f] for f in dict.fromkeys(file_ids)]
    for candidate in chosen:
        if not _safe_stage_path(candidate.project_path):
            raise PublishRefused("A selected file has a path that cannot be published safely.")
        if candidate.project_path.casefold() in {g.casefold() for g in GENERATED}:
            raise PublishRefused(f"Deselect the project's own {candidate.project_path}: "
                                 "the generated file of that name would replace it.")

    created = now or datetime.now(UTC)
    record = FileApprovalRecord(
        id=stable_id("sel", group.id, created.isoformat()), inventory_key=inventory_key, group_id=group.id,
        files=tuple(ApprovedFile(c.file_id, c.absolute_path, c.project_path, c.category, c.size_bytes, c.modified_at)
                    for c in chosen),
        approved_at=created, eligibility_status=eligibility.status, acknowledged_review=True,
        scope="copy_selected_files_for_publication",
    )
    reader = ApprovedFileReader(record, privacy)
    contents: dict[str, bytes] = {}
    for candidate in chosen:
        try:
            contents[candidate.project_path] = reader.read_bytes(candidate.file_id)
        except ReadRefused as exc:
            raise PublishRefused(f"A selected file could not be read: {exc}") from None
    contents["README.md"] = proposal.readme_markdown(draft=False).encode("utf-8")
    contents["PROVENANCE.md"] = provenance_markdown(
        proposal, chosen, visibility, ownership, course_policy, license_id, attribution).encode("utf-8")

    files = tuple(StagedFile(path, hashlib.sha256(data).hexdigest(), len(data), _git_blob(data), path in GENERATED)
                  for path, data in sorted(contents.items()))
    concerns = tuple(redact(str(c))[0] for c in proposal.attribution_concerns)
    digest = _digest(proposal.accepted_name, visibility, ownership, course_policy, license_id, attribution, concerns, files)
    stage = Stage(stable_id("st", digest, created.isoformat()), inventory_key, group.id, proposal.id,
                  proposal.accepted_name, visibility, ownership, course_policy, license_id, attribution,
                  concerns, files, digest, created.isoformat())
    root = ensure_safe_output_dir(stage_dir(inventory_key, group.id, stage.id))
    for path, data in contents.items():
        target = root.joinpath(*PurePosixPath(path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    save_stage(stage)
    return stage


def provenance_markdown(proposal: AnalysisProposal, chosen: Sequence[Candidate], visibility: str, ownership: str | None,
                        course_policy: str | None, license_id: str | None, attribution: str | None) -> str:
    """Where this came from and what is actually known. Project-relative paths only."""
    found = proposal.local_findings
    dates = sorted(c.modified_at[:10] for c in chosen if c.modified_at)
    origin = {None: "not confirmed", "not-coursework": "the publisher states this is not coursework",
              "publishing-permitted": "coursework; the publisher states the course allows publishing it"}
    lines = ["# Provenance", "", "## Original material", "",
             f"- Files: {len(chosen)}, with their original names and relative paths (listed below)",
             f"- Origin: {origin.get(course_policy, 'not confirmed')}",
             f"- Ownership stated by the publisher: {ownership or 'not confirmed'}",
             f"- License chosen by the publisher: {license_id or 'not chosen'}"]
    if dates:
        lines.append(f"- Last modified between {dates[0]} and {dates[-1]}")
    if attribution:
        lines.append(f"- Attribution: {redact(attribution)[0]}")
    lines += ["", "### Original files", ""]
    lines += [f"- `{c.project_path}`" for c in chosen]
    lines += ["", "## Generated documentation", "",
              "- `README.md` and `PROVENANCE.md` are not part of the original project. PortfolioForge generated them "
              f"for publication, with AI assistance ({proposal.provider}, {proposal.model}) and local inspection, "
              "and the publisher reviewed them.",
              "", "## Changes made for publication", "",
              "- The original files listed above were copied unchanged. None were renamed or edited.",
              "- Files that were not selected are not in this repository.", "", "## Dependencies", ""]
    dependencies = [redact(i.text)[0] for i in proposal.usage.dependencies if i.status == "verified"]
    versions = [f"`{r.setting}` {r.value} (declared by the project)" for r in (found.requirements if found else ())]
    lines += [f"- {d}" for d in (*versions, *dependencies)] or ["- None identified from the available evidence."]

    lines += ["", "## Verified and unverified instructions", ""]
    primary = proposal._primary_commands()
    for purpose, label in (("compile", "Build"), ("run", "Run"), ("test", "Test")):
        if purpose in primary:
            lines.append(f"- {label} command in the README (`{primary[purpose].command}`): supported by the project's "
                         "files, but this command was **not executed**.")
        else:
            lines.append(f"- {label}: no command is confirmed by the project's files. Anything in the README marked ⚠ "
                         "is an unverified suggestion.")
    checks = {v.action: v for v in sorted(found.validations if found else (), key=lambda v: v.finished_at)}
    for action in ("compile", "run"):
        v = checks.get(action)
        if v is None or not v.steps:
            lines.append(f"- Local {action} check: **not executed**.")
            continue
        tools = ", ".join(f"`{s.label}`" for s in v.steps)
        versions_seen = ", ".join(sorted(v.tool_versions.values())) or "version not recorded"
        lines.append(f"- Local {action} check: executed {tools} ({versions_seen}) on a temporary copy of the sources "
                     f"approved then, on {v.finished_at.date().isoformat()}, with outcome `{v.outcome}`. An exit code "
                     "does not show the program behaved correctly.")
    lines.append("- Tests: **not executed** by PortfolioForge.")
    return "\n".join(lines) + "\n"


# --- checks, always on the staged bytes -------------------------------------------------------------


def _finding(stage: Stage, check: str, kind: str, severity: str, path: str | None, line: int | None, detail: str,
             file_sha: str = "") -> Finding:
    finding_id = stable_id("pf", kind, path or "", str(line or 0), file_sha)
    return Finding(finding_id, check, kind, severity, path, line, detail, stage.resolutions.get(finding_id))


def committer_email(stage: Stage, run: Runner) -> str | None:
    """The address git would actually use for this commit: not any repository's own setting."""
    root = stage_dir(stage.inventory_key, stage.group_id, stage.id)
    return run(["git", "config", "user.email"], str(root)).stdout.strip() or None


def run_checks(stage: Stage, *, identity_terms: Iterable[str] = (), inspector=None,
               committer_email: str | None = None) -> list[Finding]:
    root = stage_dir(stage.inventory_key, stage.group_id, stage.id)
    terms = list(identity_terms)
    found: dict[tuple[str, str | None, int | None], Finding] = {}
    attributed = bool(stage.attribution and stage.attribution.strip())

    def add(finding: Finding) -> None:
        found.setdefault((finding.kind, finding.path, finding.line), finding)

    for item in stage.files:
        data = root.joinpath(*PurePosixPath(item.path).parts).read_bytes()
        name = PurePosixPath(item.path).name

        def file_finding(check: str, kind: str, severity: str, line: int | None, detail: str) -> None:
            add(_finding(stage, check, kind, severity, item.path, line, detail, item.sha256))

        parts = {p.casefold() for p in PurePosixPath(item.path).parts[:-1]}
        if rules.is_sensitive_name(name) or parts & rules.CREDENTIAL_DIR_NAMES:
            file_finding("privacy", "sensitive_file", "high", None, "a credential-looking file or location")
        for hit in check_file(item.path, data, terms):
            severity = "high" if hit.rule in _PRIVACY_RULES_HIGH else "review"
            if hit.rule == "data_file" and name.casefold().endswith(".ipynb"):
                severity = "review"  # notebooks are normal project files; their text is still scanned
            file_finding("privacy", hit.rule, severity, hit.line, f"privacy rule: {hit.rule}")
        # A file path is published too: scan it like text.
        for hit in scan_text(item.path, item.path, terms)[0]:
            if hit.kind != "absolute_path":
                file_finding("privacy", "identifier_in_path", "high", None, f"the file's path: {hit.detail}")
        try:
            if b"\0" in data:  # valid UTF-8 can still be a binary format
                raise UnicodeDecodeError("utf-8", b"", 0, 1, "contains a null byte")
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            # PDFs, documents, images and archives. Nothing can vouch for what is inside them.
            if _THIRD_PARTY_PATH.search(item.path) and not attributed:
                file_finding("attribution", "third_party_file", "high", None, "a library or binary file; attribute it")
            file_finding("privacy", "unscannable_file", "high", None,
                         "could not be scanned; open it yourself, then resolve this if it is safe to publish")
            continue
        # Possible personal identifiers block, whatever the rule's confidence. A false alarm is resolved
        # by name, with a reason, never by a general approval.
        for hit in scan_text(item.path, text, terms, inspector=inspector)[0]:
            file_finding("sensitive", hit.kind, "high", hit.line, hit.detail)
        if name.casefold().endswith(".ipynb") and _NOTEBOOK_OUTPUTS.search(text):
            file_finding("sensitive", "notebook_outputs", "high", None,
                         "saved cell outputs can hold data the patterns miss; check them, or clear them and stage again")
        if item.generated:
            if any(k[0] == "absolute_path" and k[1] == item.path for k in found) or \
                    any(k[1] == item.path and f.severity == "high" for k, f in found.items()):
                file_finding("privacy", "generated_file_leak", "high", None,
                             "a generated file contains a path or sensitive value; fix the proposal's wording")
            continue
        if _THIRD_PARTY_PATH.search(item.path) and not attributed:
            file_finding("attribution", "third_party_file", "high", None, "looks like vendored code; attribute it")
        if not _LICENSE_FILE.match(name) and not attributed and (m := _THIRD_PARTY_NOTICE.search(text)):
            file_finding("attribution", "third_party_notice", "high", text.count("\n", 0, m.start()) + 1,
                         "a copyright or license notice; attribute it or resolve it if it is your own")

    def general(check: str, kind: str, severity: str, detail: str) -> None:
        add(_finding(stage, check, kind, severity, None, None, detail))

    if stage.ownership not in OWNERSHIP:
        general("ownership", "unconfirmed_ownership", "high", "state who wrote this project (--ownership)")
    elif stage.ownership != "mine" and not attributed:
        general("attribution", "attribution_missing", "high", f"ownership is {stage.ownership}; add --attribution")
    if stage.course_policy not in COURSE_POLICY:
        general("ownership", "unconfirmed_course_policy", "high",
                "confirm this is not coursework, or that the course allows publishing (--course-policy)")
    if stage.attribution_concerns and not attributed:
        general("attribution", "ai_attribution_concern", "high",
                f"the analysis noted {len(stage.attribution_concerns)} possible third-party or template concern(s)")
    names = [PurePosixPath(f.path).name for f in stage.files]
    if not stage.license:
        general("licensing", "license_not_chosen", "high", "choose a license with --license, or --license none")
    elif stage.license == "none":
        if stage.visibility == PUBLIC:
            general("licensing", "no_license", "review", "public, with no license: others may not reuse the code")
    elif not any(_LICENSE_FILE.match(n) for n in names):
        general("licensing", "license_file_missing", "high", "a license was named, but no LICENSE file is selected")
    if stage.visibility == PUBLIC and committer_email and "noreply" not in committer_email:
        general("privacy", "public_commit_email", "high", "your git email will be visible in this public repository; "
                                                          "use your GitHub noreply address, or resolve this")
    return sorted(found.values(), key=lambda f: (not f.blocks, f.severity != "high", f.path or "", f.line or 0))


def blockers(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.blocks]


def resolve_finding(stage: Stage, findings: Iterable[Finding], finding_id: str, reason: str) -> Stage:
    match = next((f for f in findings if f.id == finding_id), None)
    if match is None:
        raise PublishRefused("No such finding for this stage.")
    if not match.resolvable:
        raise PublishRefused("This finding cannot be accepted. Fix it, or deselect the file, and stage again.")
    if not reason.strip():
        raise PublishRefused("Give a reason.")
    updated = replace(stage, resolutions={**stage.resolutions, finding_id: reason.strip()})
    save_stage(updated)
    return updated


def verify_tree(stage: Stage) -> None:
    """The staging folder must hold exactly the staged bytes, and nothing else."""
    if _digest(stage.repo_name, stage.visibility, stage.ownership, stage.course_policy, stage.license, stage.attribution,
               stage.attribution_concerns, stage.files) != stage.digest:
        raise PublishRefused("The stage record changed after it was created. Stage again.")
    root = stage_dir(stage.inventory_key, stage.group_id, stage.id)
    actual = {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file() or p.is_symlink()}
    if set(actual) != {f.path for f in stage.files}:
        raise PublishRefused("The staging folder holds different files than were staged. Stage again.")
    for item in stage.files:
        path = actual[item.path]
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != item.sha256:
            raise PublishRefused("A staged file changed after staging. Stage again.")


# --- GitHub --------------------------------------------------------------------------------------------


def github_account(run: Runner) -> str:
    result = run(["gh", "api", "user", "--jq", ".login"], None)
    login = result.stdout.strip()
    if result.code != 0 or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
        raise PublishRefused("Could not confirm your GitHub account with the GitHub CLI. Run: gh auth status")
    return login


def name_is_free(owner: str, name: str, run: Runner) -> bool:
    """Only a clear "not found" counts as free. Sends GitHub the name and nothing else."""
    result = run(["gh", "repo", "view", f"{owner}/{name}", "--json", "name"], None)
    if result.code == 0:
        return False
    if "could not resolve to a repository" in result.stderr.casefold() or "http 404" in result.stderr.casefold():
        return True
    raise PublishRefused("Could not check whether the repository name is taken, so nothing was done.")


def approve(stage: Stage, findings: Iterable[Finding], owner: str, name_free: bool, now: datetime | None = None) -> Approval:
    verify_tree(stage)
    if blockers(findings):
        raise PublishRefused("Unresolved high-risk findings block publication.")
    if not name_free:
        raise PublishRefused(f"You already have a repository named {stage.repo_name}. It will never be overwritten.")
    created = (now or datetime.now(UTC)).isoformat()
    return Approval(stable_id("pa", stage.id, stage.digest, created), stage.id, stage.digest, stage.repo_name,
                    stage.visibility, owner, created)


def publish(stage: Stage, approval: Approval | None, *, identity_terms: Iterable[str] = (), inspector=None,
            run: Runner = run_tool) -> str:
    """Create a new repository holding exactly the staged files. Returns its URL."""
    if approval is None or approval.stage_id != stage.id or approval.used_at:
        raise PublishRefused("This stage has no unused publication approval. Nothing was published.")
    if (approval.digest, approval.repo_name, approval.visibility) != (stage.digest, stage.repo_name, stage.visibility):
        raise PublishRefused("The stage differs from what was approved. Nothing was published.")
    if stage.visibility not in VISIBILITIES:
        raise PublishRefused("The stage has an invalid visibility. Nothing was published.")
    verify_tree(stage)
    if blockers(run_checks(stage, identity_terms=identity_terms, inspector=inspector,
                           committer_email=committer_email(stage, run))):
        raise PublishRefused("Unresolved high-risk findings block publication. Nothing was published.")
    owner = github_account(run)
    if owner != approval.owner:
        raise PublishRefused("The active GitHub account differs from the approved one. Nothing was published.")
    if not name_is_free(owner, stage.repo_name, run):
        raise PublishRefused(f"You already have a repository named {stage.repo_name}. It will never be overwritten.")

    root = stage_dir(stage.inventory_key, stage.group_id, stage.id)
    git = ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "core.excludesFile=",
           "-c", "core.attributesFile=", "-c", f"core.hooksPath={os.devnull}", "-c", "commit.gpgsign=false"]
    git_dir = root / ".git"

    def step(argv: list[str], failure: str) -> ToolResult:
        result = run(argv, str(root))
        if result.code != 0:
            _remove_git_dir(git_dir)
            raise PublishRefused(f"{failure} Nothing was published.")
        return result

    expected = sorted(f.path for f in stage.files)
    step([*git, "init", "--quiet", "--template=", "--object-format=sha1", "-b", "main"],
         "Could not start a local repository.")
    step([*git, "add", "--", *expected], "Could not add the staged files.")
    tracked = step([*git, "ls-files", "-z"], "Could not list the staged files.").stdout.split("\0")
    if sorted(p for p in tracked if p) != expected:
        _remove_git_dir(git_dir)
        raise PublishRefused("Git would commit different files than were approved. Nothing was published.")
    step([*git, "commit", "--quiet", "-m", "Initial publication"], "Could not commit (is your git name and email set?).")
    # The last word: what was committed must be the approved bytes, whatever happened on the way.
    listing = step([*git, "ls-tree", "-r", "-z", "HEAD"], "Could not read the commit.").stdout
    committed = {}
    for entry in filter(None, listing.split("\0")):
        meta, _, path = entry.partition("\t")
        mode, _, blob = meta.split(" ")
        committed[path] = (mode, blob)
    approved = {f.path: f.git_blob for f in stage.files}
    if {p: b for p, (_, b) in committed.items()} != approved or any(m == "120000" for m, _ in committed.values()):
        _remove_git_dir(git_dir)
        raise PublishRefused("The commit differs from the approved files. Nothing was published.")

    created = run(["gh", "repo", "create", f"{owner}/{stage.repo_name}", f"--{stage.visibility}", "--source", ".",
                   "--remote", "origin", "--push"], str(root))
    if created.code != 0:
        _remove_git_dir(git_dir)
        raise PublishRefused("The GitHub CLI could not create and push the repository. Check your GitHub account: "
                             "a new, empty repository may exist if only the push failed. Existing repositories were "
                             "not changed.")
    shown = run(["gh", "repo", "view", f"{owner}/{stage.repo_name}", "--json", "url,visibility"], None)
    _remove_git_dir(git_dir)
    try:
        details = json.loads(shown.stdout)
    except ValueError:
        details = {}
    if str(details.get("visibility", "")).casefold() != stage.visibility:
        raise PublishRefused(f"The repository was created, but its {stage.visibility} visibility could not be confirmed. "
                             "Check it on GitHub now.")
    return str(details.get("url", f"https://github.com/{owner}/{stage.repo_name}"))


def _remove_git_dir(git_dir: Path) -> None:
    """Git marks its objects read-only, which stops deletion on Windows."""
    if git_dir.is_dir():
        for path in git_dir.rglob("*"):
            if path.is_file():
                os.chmod(path, stat.S_IWRITE)
        shutil.rmtree(git_dir)


# --- local records (never in a published repository) -------------------------------------------------


def save_stage(stage: Stage) -> Path:
    return _write(group_dir(stage.inventory_key, stage.group_id), f"publish-stage-{stage.id}.json", dumps(stage))


def load_stage(inventory_key: str, group_id: str, stage_id: str) -> Stage:
    if not re.fullmatch(r"st_\w+", stage_id):
        raise PublishRefused("No such stage.")
    path = group_dir(inventory_key, group_id) / f"publish-stage-{stage_id}.json"
    if not path.is_file():
        raise PublishRefused("No such stage.")
    d = json.loads(path.read_text(encoding="utf-8"))
    return Stage(**{**d, "files": tuple(StagedFile(**f) for f in d["files"]),
                    "attribution_concerns": tuple(d["attribution_concerns"]), "resolutions": dict(d["resolutions"])})


def save_approval(approval: Approval, inventory_key: str, group_id: str) -> Path:
    return _write(group_dir(inventory_key, group_id), f"publish-approval-{approval.id}.json", dumps(approval))


def find_approval(stage: Stage) -> Approval | None:
    folder = group_dir(stage.inventory_key, stage.group_id)
    for path in sorted(folder.glob("publish-approval-*.json")):
        approval = Approval(**json.loads(path.read_text(encoding="utf-8")))
        if approval.stage_id == stage.id and approval.used_at is None:
            return approval
    return None


def format_preview(stage: Stage, findings: Sequence[Finding], *, owner: str | None, name_free: bool | None,
                   committer: str | None = None, redact_names: bool = False) -> str:
    root = stage_dir(stage.inventory_key, stage.group_id, stage.id)
    availability = {True: "available", False: "ALREADY EXISTS: will not be overwritten", None: "not checked"}[name_free]
    target = f"{owner}/{stage.repo_name}" if owner else stage.repo_name
    lines = [f"Publication preview {stage.id}{'  (findings and text hidden by --redact)' if redact_names else ''}",
             f"  Repository:  {'[hidden]' if redact_names else target} ({availability})",
             f"  Visibility:  {stage.visibility.upper()}",
             f"  Ownership:   {stage.ownership or 'NOT CONFIRMED'}",
             f"  Course:      {stage.course_policy or 'NOT CONFIRMED'}",
             f"  License:     {stage.license or 'NOT CHOSEN'}",
             f"  Files:       {len(stage.files)}"]
    if committer and not redact_names:
        lines.append(f"  Commit as:   {committer}")
    if not redact_names:
        lines += [f"    {f.path}  [{f.size_bytes:,} bytes, SHA-256 {f.sha256[:12]}]" + ("  generated" if f.generated else "")
                  for f in stage.files]
    blocking = blockers(findings)
    lines.append(f"  Findings:    {len(blocking)} blocking, {len(findings) - len(blocking)} other (values are never shown)")
    if not redact_names:
        for f in findings:
            where = f.path + (f":{f.line}" if f.line else "") if f.path else "project"
            state = "BLOCKS" if f.blocks else ("accepted" if f.resolved else "review")
            lines.append(f"    [{state}] {f.check}/{f.kind} {where}: {f.detail}"
                         + (f"  (id {f.id})" if f.blocks and f.resolvable else ""))
        for name in GENERATED:
            lines += ["", f"----- {name} -----", (root / name).read_text(encoding="utf-8").rstrip(), f"----- end {name} -----"]
    return "\n".join(lines)
