"""Repository privacy check: ``python -m portfolioforge.devtools.privacy_check``.

Scans every file Git would commit (tracked plus untracked, not ignored) for
personal data that must never enter the repository:

* inventory or grouping output (``*.jsonl``, ``report.json``, inventory-shaped JSON)
* datasets and other data files, and unusually large files
* absolute paths into a real user's home folder (``C:\\Users\\<name>``, ``/home/<name>``)
* this computer's own username and home folder, and any extra private terms
  listed in ``PORTFOLIOFORGE_PRIVATE_TERMS`` or ``private-terms.txt`` in the
  settings folder (one per line; kept outside the repository)
* secret-looking strings (private keys, access tokens)

Findings name the file, line and rule, never the matched text, so the output
itself does not repeat personal data. ``--ci`` skips the local-identity rule,
which only makes sense on a developer's own computer.

Use it as a pre-commit hook with ``git config core.hooksPath .githooks``.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable, Sequence

from ..paths import config_dir

PRIVATE_TERMS_ENV = "PORTFOLIOFORGE_PRIVATE_TERMS"
PRIVATE_TERMS_FILE = "private-terms.txt"
MAX_FILE_BYTES = 512 * 1024

#: User names that are clearly placeholders in documentation and tests.
GENERIC_USER_NAMES = frozenset(
    {"me", "you", "user", "username", "name", "student", "example", "jdoe", "alice", "bob",
     "runner", "runneradmin", "public", "default", "all users", "admin", "test", "x"}
)

FORBIDDEN_FILE_PATTERNS = ("*.jsonl", "report.json", "plan.json", "decisions.jsonl", "privacy.toml",
                           PRIVATE_TERMS_FILE)
FORBIDDEN_DIR_NAMES = frozenset({"inventories", "groupings", "reviews"})
DATA_EXTENSIONS = frozenset(
    {".csv", ".tsv", ".xlsx", ".xls", ".parquet", ".pkl", ".pickle", ".h5", ".hdf5", ".npy",
     ".npz", ".sav", ".dta", ".rds", ".rdata", ".rda", ".mat", ".zip", ".7z", ".rar", ".tar",
     ".gz", ".db", ".sqlite", ".pt", ".pth", ".joblib", ".ipynb"}
)

_WINDOWS_HOME = re.compile(r"(?i)\b[A-Z]:[\\/]+(?:Users|Documents and Settings)[\\/]+([^\\/\s\"'<>|*?:`]+)")
_POSIX_HOME = re.compile(r"(?<![\w.])/(?:home|Users)/([A-Za-z0-9._-]+)")
# Built from pieces so this file does not match its own patterns.
_SECRET_PATTERNS = (
    ("private_key", re.compile("-----BEGIN " + r"(?:[A-Z]+ )*" + "PRIVATE " + "KEY-----")),
    ("github_token", re.compile(r"\bgh[pousr]_" + r"[A-Za-z0-9]{30,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA" + r"[0-9A-Z]{16}\b")),
    ("generic_secret", re.compile(r"(?i)\b(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"\s]{12,}['\"]")),
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int | None
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}" if self.line else f"{self.path}: {self.rule}"


def local_identity_terms() -> set[str]:
    """This computer's username and home folder, plus configured private terms."""
    terms: set[str] = set()
    for candidate in (os.environ.get("USERNAME"), os.environ.get("USER"), _safe_getuser()):
        if candidate:
            terms.add(candidate)
    home = os.path.expanduser("~")
    if home and home != "~":
        terms.add(home)
        terms.add(os.path.basename(home.rstrip("\\/")))
    terms.update(t.strip() for t in os.environ.get(PRIVATE_TERMS_ENV, "").split(",") if t.strip())
    terms_file = config_dir() / PRIVATE_TERMS_FILE
    try:
        terms.update(line.strip() for line in terms_file.read_text(encoding="utf-8").splitlines()
                     if line.strip() and not line.startswith("#"))
    except OSError:
        pass
    return {t for t in terms if len(t) >= 3 and t.casefold() not in GENERIC_USER_NAMES}


def _safe_getuser() -> str | None:
    try:
        return getpass.getuser()
    except Exception:  # getuser can fail in minimal environments
        return None


def check_file(path: str, data: bytes, identity_terms: Iterable[str] = ()) -> list[Finding]:
    """Check one file's name and contents. ``path`` uses ``/`` separators."""
    findings: list[Finding] = []
    name = path.rsplit("/", 1)[-1]
    parts = path.split("/")[:-1]
    if any(fnmatch(name.casefold(), p) for p in FORBIDDEN_FILE_PATTERNS) or \
            any(p.casefold() in FORBIDDEN_DIR_NAMES for p in parts):
        findings.append(Finding(path, None, "personal_data_file"))
    if os.path.splitext(name)[1].casefold() in DATA_EXTENSIONS:
        findings.append(Finding(path, None, "data_file"))
    if len(data) > MAX_FILE_BYTES:
        findings.append(Finding(path, None, "large_file"))
    if b"\0" in data[:8192]:
        return findings  # binary: name-based rules only
    text = data.decode("utf-8", errors="replace")

    if name.casefold().endswith(".json") and _looks_like_inventory_json(text):
        findings.append(Finding(path, None, "inventory_data"))
    term_patterns = [
        re.compile(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])", re.IGNORECASE)
        for t in identity_terms
    ]
    for number, line in enumerate(text.splitlines(), start=1):
        for match in (*_WINDOWS_HOME.finditer(line), *_POSIX_HOME.finditer(line)):
            if match.group(1).casefold() not in GENERIC_USER_NAMES:
                findings.append(Finding(path, number, "absolute_home_path"))
                break
        if any(p.search(line) for p in term_patterns):
            findings.append(Finding(path, number, "local_identity"))
        for rule, pattern in _SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(Finding(path, number, rule))
    return findings


def _looks_like_inventory_json(text: str) -> bool:
    try:
        data = json.loads(text)
    except ValueError:
        return False
    keys = set(data) if isinstance(data, dict) else set()
    return bool({"root_id", "relative_path"} <= keys
                or {"report", "files"} <= keys
                or {"schema_version", "summary", "roots"} <= keys)


def committable_files(root: Path) -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root, capture_output=True, check=True,
    ).stdout
    return sorted({p for p in output.decode("utf-8", errors="surrogateescape").split("\0") if p})


def run_check(root: Path, *, include_identity: bool) -> list[Finding]:
    terms = local_identity_terms() if include_identity else set()
    findings: list[Finding] = []
    for relative in committable_files(root):
        file = root / relative
        if not file.is_file():
            continue  # deleted but still tracked
        findings.extend(check_file(relative, file.read_bytes(), terms))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m portfolioforge.devtools.privacy_check",
                                     description="Check the repository for personal data before committing.")
    parser.add_argument("--root", default=".", help="repository root (default: current folder)")
    parser.add_argument("--ci", action="store_true", help="skip the local-identity rule (for CI)")
    args = parser.parse_args(argv)
    try:
        findings = run_check(Path(args.root), include_identity=not args.ci)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"privacy check could not run: {type(exc).__name__}", file=sys.stderr)
        return 2
    if findings:
        print("Privacy check FAILED. Matched text is not shown, to avoid repeating it:")
        print("\n".join(f"  {f}" for f in findings))
        return 1
    print("Privacy check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
