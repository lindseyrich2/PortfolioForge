"""Read-only GitHub metadata through the GitHub CLI you're already signed in to.

Every request is ``gh api -X GET`` to an endpoint on a short list, and no endpoint on it returns file
contents: the blobs, contents and compare endpoints are deliberately absent. No token is handled here.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Sequence

MAX_PAGES = 50
_NAME = r"[A-Za-z0-9_.-]+"
_ALLOWED = re.compile(rf"(user|user/repos|repos/{_NAME}/{_NAME}(/contributors|/commits|/git/trees/[^/?#\s]+)?)\Z")
_FULL_NAME = re.compile(rf"{_NAME}/{_NAME}\Z")
AFFILIATION = "owner,collaborator,organization_member"


class GitHubError(ValueError):
    """Messages never contain repository names, paths or GitHub's own response text."""


@dataclass(frozen=True)
class GhResult:
    code: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str]], GhResult]


def run_gh(argv: Sequence[str]) -> GhResult:
    try:
        done = subprocess.run(list(argv), shell=False, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GhResult(127, "", type(exc).__name__)
    return GhResult(done.returncode, done.stdout, done.stderr)


def is_full_name(value: str) -> bool:
    parts = value.split("/")
    return bool(_FULL_NAME.match(value)) and not value.startswith("-") and not {".", ".."} & set(parts)


class GitHubClient:
    def __init__(self, runner: Runner = run_gh) -> None:
        self._runner = runner

    def _get(self, endpoint: str, **query: Any) -> Any:
        """The parsed response. None for "nothing here": an empty repository, or a missing branch or file."""
        if not _ALLOWED.match(endpoint) or {".", ".."} & set(endpoint.split("/")):
            raise GitHubError("That GitHub request isn't on PortfolioForge's read-only list.")
        command = ["gh", "api", "-X", "GET", endpoint]
        for key, value in query.items():
            command += ["-f", f"{key}={value}"]
        result = self._runner(command)
        if result.code == 0:
            return json.loads(result.stdout) if result.stdout.strip() else None
        if result.code == 127:
            raise GitHubError("The GitHub CLI isn't available. Install it and run: gh auth login")
        status = re.search(r"HTTP (\d{3})", result.stderr)
        code = status.group(1) if status else None
        if code in ("404", "409"):
            return None
        if code == "401" or "gh auth login" in result.stderr:
            raise GitHubError("The GitHub CLI isn't signed in. Run: gh auth login")
        raise GitHubError(f"A GitHub request failed ({'HTTP ' + code if code else 'no status'}).")

    def _pages(self, endpoint: str, **query: Any) -> list[dict]:
        items: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            batch = self._get(endpoint, per_page=100, page=page, **query) or []
            items += batch
            if len(batch) < 100:
                break
        return items

    def login(self) -> str:
        return self._get("user")["login"]

    def repositories(self) -> list[dict]:
        return self._pages("user/repos", affiliation=AFFILIATION)

    def repository(self, full_name: str) -> dict | None:
        return self._get(f"repos/{_checked(full_name)}")

    def tree(self, full_name: str, branch: str) -> dict | None:
        """Paths, modes, sizes and blob hashes of one branch. An empty repository returns None."""
        encoded = branch.replace("%", "%25").replace("/", "%2F")
        return self._get(f"repos/{_checked(full_name)}/git/trees/{encoded}", recursive=1)

    def contributors(self, full_name: str) -> list[dict] | None:
        """Commit counts per contributor. None if GitHub won't compute them, such as for a very large history."""
        try:
            found = self._pages(f"repos/{_checked(full_name)}/contributors", anon=1)
        except GitHubError:
            return None
        return found

    def file_commits(self, full_name: str, path: str) -> list[dict]:
        return self._get(f"repos/{_checked(full_name)}/commits", path=path, per_page=100) or []


def _checked(full_name: str) -> str:
    if not is_full_name(full_name):
        raise GitHubError("That isn't an OWNER/NAME repository.")
    return full_name
