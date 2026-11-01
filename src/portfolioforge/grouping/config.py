"""Grouping settings: generic defaults, optionally layered with a user TOML file.

Nothing here is specific to one person's coursework. Users extend the
vocabularies or tune weights in ``grouping.toml`` in their settings folder::

    [vocabulary]
    assignment_words = ["tarea", "practica"]     # added to the defaults
    container_names = ["Semester Work"]
    known_third_party_names = ["some-library"]

    [patterns]
    course_code = '(?i)\\b([A-Z]{2,5})[ _-]?(\\d{3,4}[A-Z]?)\\b'

    [weights]
    same_assignment_number = 3.0

    [thresholds]
    cluster_link = 2.5
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from ..paths import config_dir

GROUPING_FILE_NAME = "grouping.toml"


class GroupingConfigError(ValueError):
    pass


DEFAULT_WEIGHTS: dict[str, float] = {
    # folder-level project evidence
    "version_control": 2.0,
    "ide_project": 1.5,
    "dependency_file": 1.5,
    "readme": 1.0,
    "virtual_env": 1.5,
    "build_output": 0.5,
    "license_or_ignore": 0.5,
    "structured_layout": 1.0,
    "assignment_name": 2.0,
    "shared_folder": 1.5,
    # loose-file clustering
    "same_assignment_number": 3.0,
    "different_assignment_number": -4.0,
    "same_base_name": 2.0,
    "shared_token": 1.0,
    "shared_token_cap": 2.5,
    "close_in_time": 1.0,
    "very_close_in_time": 1.5,
    "complementary_types": 0.5,
    # cross-folder relationships
    "rare_shared_token": 1.0,
    "rare_shared_token_cap": 3.0,
    "same_course_code": 1.5,
    "overlapping_time": 1.0,
    "report_and_code": 0.5,
    # flags
    "third_party_vcs": 1.0,
    "third_party_community_files": 1.5,
    "third_party_uniform_times": 1.5,
    "third_party_known_name": 3.0,
    "template_name": 2.0,
    "template_uniform_times": 1.0,
}

DEFAULT_THRESHOLDS: dict[str, float] = {
    "project_folder": 1.5,
    "nested_project": 2.5,
    "cluster_link": 2.5,
    "relationship": 3.0,
    "same_course_relationship": 2.0,
    "merge_suggestion": 4.0,
    "duplicate_similarity": 0.8,
    "variant_similarity": 0.8,
    "third_party_flag": 2.5,
    "template_flag": 2.0,
    "confidence_bias": 2.0,
    "high_band": 0.8,
    "medium_band": 0.5,
    "max_token_document_ratio": 0.2,
}


@dataclass(frozen=True)
class GroupingConfig:
    assignment_words: frozenset[str] = frozenset({
        "hw", "homework", "assignment", "assign", "asg", "lab", "project", "proj", "exercise",
        "ex", "ps", "pset", "problemset", "quiz", "midterm", "final", "exam", "milestone",
        "checkpoint", "tarea", "practica", "práctica", "ubung", "übung", "tp", "td",
    })
    """Words that mark an assignment, optionally followed by a number (``hw3``, ``Lab 02``)."""
    numbered_only_words: frozenset[str] = frozenset({"ex", "ps", "tp", "td", "asg", "assign"})
    """Short words that count only when a number follows (``ex3``, not ``ex``)."""
    course_code_pattern: str = r"(?<![A-Za-z0-9])([A-Za-z]{2,5})[ _-]?(\d{3,4}[A-Za-z]?)(?![A-Za-z0-9])"
    course_code_excluded_words: frozenset[str] = frozenset({
        "fall", "spring", "summer", "winter", "autumn", "year", "week", "day", "page", "room", "version", "ver",
        "rev", "vol", "part", "chapter", "ch", "windows", "win", "office", "python", "java", "lab", "hw",
        "project", "proj", "test", "data", "file", "img", "image", "photo", "scan", "copy", "backup", "final",
    })
    """Words that look like a course prefix but are not ("Fall 2021", "Office 2016")."""
    term_pattern: str =r"(?i)\b(fall|spring|summer|winter|autumn)[ _-]?(\d{2}|\d{4})\b|\bsemester\b|\bterm\b"
    collection_words: frozenset[str] = frozenset({"course", "class", "module"})
    """Folder-name words that make a container a course collection."""
    container_names: frozenset[str] = frozenset({
        "desktop", "documents", "my documents", "downloads", "onedrive", "dropbox", "google drive",
        "icloud drive", "home", "pictures", "music", "videos", "misc", "temp", "tmp", "archive",
        "archives", "old", "backup", "backups", "pycharmprojects", "ideaprojects", "netbeansprojects",
        "eclipse-workspace", "workspace", "source", "repos", "projects", "code", "dev", "src-all",
        "school", "college", "university", "uni", "classes", "courses", "coursework", "assignments",
        "homework", "labs", "notebooks", "colab notebooks",
    })
    """Folders that collect unrelated work. They never become a project themselves."""
    structural_names: frozenset[str] = frozenset({
        "src", "source", "lib", "include", "data", "dataset", "datasets", "docs", "doc", "notebooks",
        "models", "model", "utils", "util", "helpers", "tests", "test", "scripts", "figures", "figs",
        "images", "img", "results", "output", "outputs", "reports", "report", "r", "man", "inst",
        "assets", "static", "templates", "config", "resources", "res", "main", "java", "python",
    })
    """Subfolder names that are part of a project's layout, not separate projects."""
    generic_name_patterns: tuple[str, ...] = (
        r"untitled\d*", r"untitled[ _-]?\d*", r"main", r"test\d*", r"notebook\d*", r"new folder( \(\d+\))?",
        r"pythonproject\d*", r"javaapplication\d*", r"consoleapp\d*", r"wpfapp\d*", r"windowsformsapp\d*",
        r"project\d*", r"script\d*", r"code\d*", r"file\d*", r"document\d*", r"copy of .*", r"temp\d*",
        r"index", r"app", r"solution\d*",
    )
    stopwords: frozenset[str] = frozenset({
        "the", "a", "an", "of", "and", "or", "for", "to", "in", "on", "with", "by", "my", "our",
        "final", "project", "projects", "report", "reports", "copy", "new", "old", "v", "version",
        "draft", "hw", "homework", "lab", "labs", "assignment", "assignments", "data", "src", "main",
        "test", "tests", "code", "file", "files", "doc", "docs", "notes", "untitled", "python", "java",
        "notebook", "notebooks", "script", "scripts", "part", "submission", "solution", "solutions",
        "fall", "spring", "summer", "winter", "week", "day", "class", "course", "ipynb", "readme",
    })
    """Tokens too common to suggest that two things are related."""
    copy_patterns: tuple[str, ...] = (
        r"(?i)\s*-\s*copy(\s*\(\d+\))?$", r"(?i)\s*\(\d+\)$", r"(?i)^copy of\s+", r"(?i)[_ -](old|backup|bak|copy)\d*$",
    )
    template_words: frozenset[str] = frozenset({"template", "starter", "skeleton", "boilerplate", "scaffold"})
    known_third_party_names: frozenset[str] = frozenset()
    """Names the user knows are third-party projects (added in grouping.toml)."""
    cluster_time_window_seconds: float = 3 * 24 * 3600
    tight_time_window_seconds: float = 3600
    uniform_time_window_seconds: float = 120
    uniform_time_fraction: float = 0.9
    uniform_time_min_files: int = 10
    relationship_time_window_seconds: float = 30 * 24 * 3600
    large_dataset_bytes: int = 100 * 1024 * 1024
    max_files_per_cluster_folder: int = 400
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))

    def w(self, key: str) -> float:
        return self.weights[key]

    def t(self, key: str) -> float:
        return self.thresholds[key]

    def digest(self) -> str:
        data = {k: sorted(v) if isinstance(v, frozenset) else v for k, v in asdict(self).items()}
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


def default_grouping_file() -> Path:
    return config_dir() / GROUPING_FILE_NAME


_VOCAB_KEYS = {"assignment_words", "collection_words", "container_names", "structural_names",
               "stopwords", "template_words", "known_third_party_names", "course_code_excluded_words"}
_PATTERN_KEYS = {"course_code": "course_code_pattern", "term": "term_pattern"}


def load_grouping_config(path: str | os.PathLike[str] | None = None) -> GroupingConfig:
    """Defaults, layered with the user's grouping.toml if present (or ``path``)."""
    explicit = path is not None
    file = Path(path) if explicit else default_grouping_file()
    if not file.is_file():
        if explicit:
            raise GroupingConfigError(f"Grouping settings file not found: {file}")
        return GroupingConfig()
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise GroupingConfigError(f"Could not read grouping settings ({type(exc).__name__}).") from exc
    return config_from_dict(data)


def config_from_dict(data: dict[str, Any], base: GroupingConfig | None = None) -> GroupingConfig:
    config = base or GroupingConfig()
    unknown = set(data) - {"vocabulary", "patterns", "weights", "thresholds"}
    if unknown:
        raise GroupingConfigError(f"Unknown grouping settings sections: {', '.join(sorted(unknown))}")
    changes: dict[str, Any] = {}

    vocab = _table(data, "vocabulary")
    if bad := set(vocab) - _VOCAB_KEYS:
        raise GroupingConfigError(f"Unknown vocabulary keys: {', '.join(sorted(bad))}")
    for key, values in vocab.items():
        if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
            raise GroupingConfigError(f"vocabulary.{key} must be a list of non-empty strings.")
        changes[key] = getattr(config, key) | {v.strip().casefold() for v in values}

    patterns = _table(data, "patterns")
    if bad := set(patterns) - set(_PATTERN_KEYS):
        raise GroupingConfigError(f"Unknown pattern keys: {', '.join(sorted(bad))}")
    for key, value in patterns.items():
        try:
            re.compile(value)
        except (re.error, TypeError) as exc:
            raise GroupingConfigError(f"patterns.{key} is not a valid regular expression.") from exc
        changes[_PATTERN_KEYS[key]] = value

    for section, defaults in (("weights", config.weights), ("thresholds", config.thresholds)):
        table = _table(data, section)
        if bad := set(table) - set(defaults):
            raise GroupingConfigError(f"Unknown {section}: {', '.join(sorted(bad))}")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in table.values()):
            raise GroupingConfigError(f"{section} values must be numbers.")
        if table:
            changes[section] = {**defaults, **{k: float(v) for k, v in table.items()}}
    return replace(config, **changes)


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise GroupingConfigError(f"[{key}] must be a table.")
    return value
