"""Name features used as grouping evidence. Pure functions over names only."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .config import GroupingConfig

_SPLIT = re.compile(r"[\s_\-.()\[\]{},;+&@#!]+")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_DIGITS = re.compile(r"(?<=\D)(?=\d)|(?<=\d)(?=\D)")


def tokenize(name: str) -> tuple[str, ...]:
    """``"ML-Project_SpamFilter v2"`` → ``("ml", "project", "spam", "filter", "v", "2")``."""
    tokens: list[str] = []
    for chunk in _SPLIT.split(name):
        for piece in _CAMEL.split(chunk):
            tokens.extend(p.casefold() for p in _DIGITS.split(piece) if p)
    return tuple(tokens)


@lru_cache(maxsize=64)
def _compiled(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p) for p in patterns)


def strip_copy_markers(name: str, config: GroupingConfig) -> tuple[str, bool]:
    """Remove ``- Copy``, ``(2)``, ``Copy of``, ``_old`` and similar. Returns (name, was_copy)."""
    stripped, changed = name.strip(), False
    for _ in range(3):  # "X - Copy (2)" and "Copy of X (1)"
        before = stripped
        for pattern in _compiled(config.copy_patterns):
            stripped = pattern.sub("", stripped).strip()
        if stripped == before:
            break
        changed = True
    return (stripped or name), changed


@dataclass(frozen=True, slots=True)
class AssignmentMarker:
    word: str
    number: str | None

    @property
    def key(self) -> str:
        """``hw-2`` style key; numbers are normalized (``02`` → ``2``)."""
        return f"{_canonical_word(self.word)}-{self.number}" if self.number else _canonical_word(self.word)


_WORD_ALIASES = {"homework": "hw", "assign": "assignment", "asg": "assignment", "proj": "project",
                 "pset": "ps", "problemset": "ps", "práctica": "practica", "übung": "ubung"}


def _canonical_word(word: str) -> str:
    return _WORD_ALIASES.get(word, word)


def assignment_marker(name: str, config: GroupingConfig) -> AssignmentMarker | None:
    """Detect ``hw3``, ``Homework 03``, ``Lab-2``, ``Final Project``, ``tarea1`` (configurable).

    IDE default names such as ``pythonProject4`` are generic, not assignments.
    """
    if is_generic_name(name, config):
        return None
    tokens = tokenize(strip_copy_markers(name, config)[0])
    for i, token in enumerate(tokens):
        if token not in config.assignment_words:
            continue
        number = tokens[i + 1] if i + 1 < len(tokens) and tokens[i + 1].isdigit() else None
        if number is None and token in config.numbered_only_words:
            continue
        return AssignmentMarker(token, str(int(number)) if number else None)
    return None


def course_codes(name: str, config: GroupingConfig) -> tuple[str, ...]:
    """Course-code hints such as ``CS 101`` or ``STAT 200``. Hints only, never decisive."""
    found = []
    for match in re.finditer(config.course_code_pattern, name):
        letters, digits = match.group(1), match.group(2)
        if letters.casefold() in config.course_code_excluded_words:
            continue
        found.append(f"{letters.upper()} {digits.upper()}")
    return tuple(dict.fromkeys(found))


def is_term_name(name: str, config: GroupingConfig) -> bool:
    return re.search(config.term_pattern, name) is not None


def is_generic_name(stem: str, config: GroupingConfig) -> bool:
    text = strip_copy_markers(stem, config)[0].casefold().strip()
    return any(p.fullmatch(text) for p in _compiled(tuple(f"(?i){p}" for p in config.generic_name_patterns)))


def distinctive_tokens(name: str, config: GroupingConfig) -> frozenset[str]:
    """Tokens that could indicate a relationship: no stopwords, numbers or very short tokens."""
    return frozenset(
        t for t in tokenize(strip_copy_markers(name, config)[0])
        if len(t) >= 3 and not t.isdigit() and t not in config.stopwords and t not in config.assignment_words
    )


def file_role(category: str, name: str) -> str:
    """Role of a file inside a group, from its category and name only."""
    lower = name.casefold()
    if category in ("code",):
        return "code"
    if category == "notebook":
        return "notebook"
    if category == "presentation":
        return "slides"
    if category in ("data", "spreadsheet"):
        return "data"
    if category == "document":
        if lower.startswith(("readme", "requirements", "license")):
            return "config" if lower.startswith("requirements") else "other"
        return "report"
    return "other"
