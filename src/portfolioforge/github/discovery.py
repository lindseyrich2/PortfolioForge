"""Repository listings into an ordinary inventory, plus attribution signals for review.

The listing is shaped like a Drive listing so the Drive builder, which already applies the local
scanner's rules and makes names safe, can reuse it. Each repository becomes one folder, which grouping
usually treats as one project. Records point at ``github://`` locations that can't be opened, so no later
stage can read a file from here: to analyze one, clone it and scan it locally.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from ..drive.client import FOLDER_MIME, SHORTCUT_MIME, DriveEntry
from ..drive.discovery import DriveOptions, Listing, Target, _short, is_notebook, safe_name
from ..drive.duplicates import CrossSourceMatch
from ..grouping.models import GroupingRun
from ..inventory.models import FileRecord, Inventory, RootOrigin, ScanRoot
from ..inventory.sinks import GITHUB_REPOS_NAME
from .client import GitHubClient

LIKELY_ORIGINAL, SHARED, THIRD_PARTY, UNKNOWN = "likely_original", "shared", "third_party", "unknown"
#: For a group spanning several repositories, the label that needs the most care wins.
_CAUTION = (THIRD_PARTY, UNKNOWN, SHARED, LIKELY_ORIGINAL)
NOT_PROOF = "commits and ownership don't prove who wrote the code"
ROOT_ID = "github-root"
_SYMLINK_MODE = "120000"
_PROVENANCE = "provenance.md"
_SELF_LAYOUT = "src/portfolioforge/inventory"
_REMOTE = re.compile(r"github\.com[:/]+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", re.IGNORECASE)


@dataclass
class RepoInfo:
    full_name: str
    folder: str
    owner: str
    private: bool
    fork: bool
    parent: str | None = None
    template: str | None = None
    license: str | None = None
    mine: int = 0
    others: int = 0
    unattributed: int = 0
    other_logins: list[str] = field(default_factory=list)
    contributors_known: bool = False
    submodules: int = 0
    published_by_portfolioforge: bool = False
    incomplete_listing: bool = False
    label: str = UNKNOWN
    reasons: list[str] = field(default_factory=list)
    file_commits: dict[str, dict] = field(default_factory=dict)
    """Notebook path to commit counts under your login, others' and unlinked. Counts only."""


def classify(info: RepoInfo, login: str) -> tuple[str, list[str]]:
    """A cautious label from metadata. "likely_original" still needs your own confirmation."""
    owned = info.owner.casefold() == login.casefold()
    reasons = ["you own this repository" if owned else "someone else owns this repository"]
    if info.fork:
        reasons.append("it's a fork")
    if info.template:
        reasons.append("it was created from a template")
    if info.submodules:
        reasons.append("it includes other repositories as submodules")
    if not info.contributors_known:
        reasons.append("commit counts weren't available")
        return UNKNOWN, reasons
    reasons.append(f"commits: {info.mine} under your login, {info.others} by others, {info.unattributed} not linked to an account")
    if info.fork:
        return (SHARED if info.mine else THIRD_PARTY), reasons
    if not owned:
        if info.mine:
            return SHARED, reasons
        return (UNKNOWN if info.unattributed else THIRD_PARTY), reasons
    if info.others or info.template or info.submodules:
        return SHARED, reasons
    if not info.mine and not info.unattributed:
        return UNKNOWN, reasons
    return LIKELY_ORIGINAL, reasons


def _contributions(info: RepoInfo, contributors: list[dict] | None, login: str) -> None:
    if contributors is None:
        return
    info.contributors_known = bool(contributors)
    logins: set[str] = set()
    for person in contributors:
        count = person.get("contributions", 0)
        if person.get("type") == "Anonymous":  # the name and email GitHub returns here are deliberately not kept
            info.unattributed += count
        elif person.get("login", "").casefold() == login.casefold():
            info.mine += count
        else:
            info.others += count
            logins.add(person.get("login", "unknown"))
    info.other_logins = sorted(logins)


def _file_counts(commits: list[dict], login: str) -> tuple[dict, datetime | None]:
    counts = {"mine": 0, "others": 0, "unattributed": 0}
    latest = None
    for commit in commits:
        author = (commit.get("author") or {}).get("login")
        counts["unattributed" if not author else "mine" if author.casefold() == login.casefold() else "others"] += 1
        stamp = ((commit.get("commit") or {}).get("author") or {}).get("date")
        if stamp and latest is None:  # newest first
            try:
                latest = datetime.fromisoformat(stamp)
            except ValueError:
                pass
    return counts, latest


@dataclass
class GitHubListing:
    listing: Listing
    repos: list[RepoInfo]
    left_out_private: int = 0
    left_out_self: int = 0
    empty: int = 0


def collect(client: GitHubClient, login: str, repositories: Iterable[dict], options: DriveOptions, *, max_commit_lookups: int = 200,
            clock: Callable[[], float] = time.monotonic) -> GitHubListing:
    """Fetch metadata only. Everything is fetched before anything is written, so a failure leaves no partial inventory."""
    root = ScanRoot(f"gh_{_short(login)}", "github://", RootOrigin.GITHUB, "GitHub")
    entries: dict[str, DriveEntry] = {}
    result = GitHubListing(Listing((Target(ROOT_ID, root),), entries, defaultdict(list), False), [])
    budget = max_commit_lookups
    start = clock()
    for repo in sorted(repositories, key=lambda r: r["full_name"].casefold()):
        limit = options.limits.time_budget_seconds
        if limit is not None and clock() - start >= limit:
            result.listing.stop_reason = "time_budget"
            break
        if repo["name"].casefold() in options.private_names or repo["name"].casefold() in options.exclude_names:
            result.left_out_private += 1
            continue
        branch = repo.get("default_branch")
        tree = client.tree(repo["full_name"], branch) if branch else None
        if tree is None:
            result.empty += 1
            continue
        items = tree.get("tree", [])
        paths = {i["path"].casefold() for i in items}
        if "pyproject.toml" in paths and _SELF_LAYOUT in paths:  # the same rule the local scanner applies to its own code
            result.left_out_self += 1
            continue
        detail = client.repository(repo["full_name"]) or repo
        info = RepoInfo(
            full_name=repo["full_name"], folder=safe_name(f"{repo['name']} ({repo['owner']['login']})"),
            owner=repo["owner"]["login"], private=bool(repo.get("private")), fork=bool(detail.get("fork")),
            parent=(detail.get("parent") or {}).get("full_name"),
            template=(detail.get("template_repository") or {}).get("full_name"),
            license=(detail.get("license") or {}).get("spdx_id"),
            incomplete_listing=bool(tree.get("truncated")),
            submodules=sum(i.get("type") == "commit" for i in items),
            published_by_portfolioforge=_PROVENANCE in paths,
        )
        _contributions(info, client.contributors(repo["full_name"]), login)
        repo_id = f"{repo['full_name']}:"
        _add(result.listing, DriveEntry(repo_id, info.folder, FOLDER_MIME, (ROOT_ID,)))
        folders = {"": repo_id}
        for item in items:
            if item.get("type") != "blob":
                continue
            parts = item["path"].split("/")
            parent = _ensure_folders(result.listing, folders, repo_id, parts[:-1])
            entry = DriveEntry(repo_id + item["path"], parts[-1], SHORTCUT_MIME if item.get("mode") == _SYMLINK_MODE else "text/plain",
                               (parent,), item.get("size"), item.get("sha"))
            if budget > 0 and is_notebook(entry) and not _in_private_folder(parts[:-1], options):
                budget -= 1
                counts, latest = _file_counts(client.file_commits(repo["full_name"], item["path"]), login)
                info.file_commits[item["path"]] = counts
                entry = DriveEntry(entry.id, entry.name, entry.mime_type, entry.parents, entry.size, entry.md5, latest)
            _add(result.listing, entry)
        if info.incomplete_listing:
            result.listing.stop_reason = "incomplete_listing"
        info.label, info.reasons = classify(info, login)
        result.repos.append(info)
    return result


def _in_private_folder(parts: Iterable[str], options: DriveOptions) -> bool:
    return any(p.casefold() in options.private_names for p in parts)


def _add(listing: Listing, entry: DriveEntry) -> None:
    listing.entries[entry.id] = entry
    listing.children[entry.parents[0]].append(entry)


def _ensure_folders(listing: Listing, folders: dict[str, str], repo_id: str, parts: list[str]) -> str:
    current, parent = "", repo_id
    for part in parts:
        current = f"{current}/{part}" if current else part
        if current not in folders:
            folders[current] = f"{repo_id}{current}/"
            _add(listing, DriveEntry(folders[current], part, FOLDER_MIME, (parent,)))
        parent = folders[current]
    return parent


def location(entry: DriveEntry, relative: str, name: str) -> str:
    """Where a record says its file is. Deliberately never a real file: nothing can open it."""
    return "github://" + relative


def write_repos(output_dir: Path, login: str, repos: Iterable[RepoInfo]) -> None:
    body = {"login": login, "repos": [asdict(r) for r in repos]}
    (output_dir / GITHUB_REPOS_NAME).write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")


def attribution_concerns(run: GroupingRun, inventory: Inventory, repos: Iterable[RepoInfo]) -> dict[str, list[str]]:
    """For the batch review. Names no repository, person or file, so they are safe to show under --redact."""
    by_folder = {r.folder: r for r in repos}
    files = {f.id: f for f in inventory.files}
    concerns: dict[str, list[str]] = {}
    for group in run.groups:
        found = {by_folder[files[m.file_id].path_parts[0]].full_name: by_folder[files[m.file_id].path_parts[0]]
                 for m in group.members if m.file_id in files and files[m.file_id].path_parts
                 and files[m.file_id].path_parts[0] in by_folder}
        if not found:
            continue
        info = min(found.values(), key=lambda r: _CAUTION.index(r.label))
        text = f"attribution: {info.label}: {'; '.join(info.reasons)}. Review before use; {NOT_PROOF}."
        unlinked = sum(not c["mine"] for r in found.values() for c in r.file_commits.values())
        if unlinked:
            text += f" {unlinked} notebook(s) show no commits under your login."
        if any(r.published_by_portfolioforge for r in found.values()):
            text += " It looks already published by PortfolioForge."
        concerns[group.id] = [text]
    return concerns


def read_remote(directory: str) -> tuple[str, str] | None:
    """The GitHub owner and name in a clone's ``origin`` remote. Reads only ``.git/config``; a URL's credentials are never kept."""
    parser = configparser.RawConfigParser(strict=False)
    try:
        with open(os.path.join(directory, ".git", "config"), encoding="utf-8", errors="replace") as handle:
            parser.read_string(handle.read(65_536))
    except (OSError, configparser.Error):
        return None
    for section in ('remote "origin"', 'remote "upstream"'):
        if parser.has_option(section, "url"):
            match = _REMOTE.search(parser.get(section, "url").strip())
            if match:
                return match.group(1), match.group(2)
    return None


def local_clone_matches(local: Inventory, local_run: GroupingRun, github: Inventory, github_run: GroupingRun, repos: Iterable[RepoInfo],
                        login: str) -> tuple[list[CrossSourceMatch], dict[str, list[str]]]:
    """Local folders whose Git remote is a discovered repository, and those whose remote is someone else's unlisted one."""
    by_name = {r.full_name.casefold(): r for r in repos}
    records = {f.id: f for f in github.files}
    repo_groups: dict[str, str] = {}
    for group in github_run.groups:
        for member in group.members:
            record = records.get(member.file_id)
            if record and record.path_parts:
                repo_groups.setdefault(record.path_parts[0], group.id)
    matches: list[CrossSourceMatch] = []
    concerns: dict[str, list[str]] = {}
    for directory in local.directories:
        if ".git" not in directory.marker_dirs:
            continue
        remote = read_remote(directory.absolute_path)
        if remote is None:
            continue
        group = _group_under(local, local_run, directory.root_id, directory.relative_path)
        if group is None:
            continue
        info = by_name.get(f"{remote[0]}/{remote[1]}".casefold())
        if info is not None and info.folder in repo_groups:
            matches.append(CrossSourceMatch(repo_groups[info.folder], group, "possible_duplicate",
                                            f"this local clone's Git remote is the repository ({info.label})"))
        elif info is None and remote[0].casefold() != login.casefold():
            concerns.setdefault(group, []).append(
                f"attribution: {UNKNOWN}: its Git remote is a GitHub repository not among those you can list. Review before use; {NOT_PROOF}.")
    return matches, concerns


def _group_under(inventory: Inventory, run: GroupingRun, root_id: str, relative: str) -> str | None:
    prefix = f"{relative}/" if relative else ""
    wanted = {f.id for f in _files_under(inventory.files, root_id, prefix)}
    return next((g.id for g in run.groups if any(m.file_id in wanted for m in g.members)), None)


def _files_under(files: Iterable[FileRecord], root_id: str, prefix: str) -> Iterable[FileRecord]:
    return (f for f in files if f.root_id == root_id and f.relative_path.startswith(prefix))
