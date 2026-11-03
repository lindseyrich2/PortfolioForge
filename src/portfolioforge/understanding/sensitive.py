"""Finding credentials, identifiers and personal sample data before publication.

Findings give the file, line and kind of match, never the matched text and never a hash
of it (short values are easy to reverse), so saved results and terminal output do not
repeat what they flag. Pattern matching misses things and raises false alarms: a clean
scan only speaks for the files that were read, and every finding is for you to judge.

These rules apply to every project type. An inspector can add its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

from ..devtools.privacy_check import _SECRET_PATTERNS
from .models import SensitiveFinding
from .redact import (
    _AUTHOR_LINE,
    _CARD,
    _EMAIL,
    _NAME_COMMENT,
    _PHONE,
    _POSIX_HOME,
    _SSN,
    _UNC_PATH,
    _WINDOWS_PATH,
    is_card_number,
)

if TYPE_CHECKING:
    from .inspectors.base import ProjectInspector

MAX_FINDINGS_PER_FILE = 50
#: Longer lines (a notebook's embedded image, minified data) are too slow to match and are
#: reported as not scanned instead of being silently passed.
MAX_SCANNED_LINE_CHARS = 5_000
_CONFIG_SUFFIXES = (".properties", ".cfg", ".ini")
_PLACEHOLDER_VALUES = frozenset({
    "", "password", "passwd", "pwd", "secret", "token", "changeme", "change_me", "changeit", "example", "test",
    "none", "null", "todo", "your_password", "yourpassword", "your-password", "api_key", "apikey", "your_api_key",
    "placeholder",
})
_CREDENTIAL_WORDS = (r"(?:password|passwd|pwd|passphrase|secret|api[_.-]?key|apikey|access[_.-]?key|"
                     r"auth[_.-]?token|token|credential)")
# Double or single quotes, either of which may be escaped (as in a notebook's JSON).
_ASSIGNED_LITERAL = re.compile(
    rf"(?i)(?<![\w.-])[\w.-]*{_CREDENTIAL_WORDS}[\w.-]*\s*(?::|=|\+=)\s*\\?[\"']([^\"'\\\n]*)\\?[\"']"
)
_PROPERTY_VALUE = re.compile(rf"(?i)^\s*[\w.-]*{_CREDENTIAL_WORDS}[\w.-]*\s*[=:]\s*(\S.*?)\s*$")
_PRIVATE_IP = re.compile(r"(?<![\d.])(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?![\d.])")
_STUDENT_ID = re.compile(
    r"(?i)\b\w*(?:student|employee|patient|member)[ _-]?(?:id|no|number|num)\b\s*[:=#]?\s*\"?\w*\d{4,}"
)
_BIRTH_DATE = re.compile(r"(?i)\b(?:dob|birth[ _-]?date|date[ _-]?of[ _-]?birth)\b.*\d{1,4}[/-]\d{1,2}[/-]\d{1,4}")
_STREET_ADDRESS = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s+){1,3}(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Boulevard|Blvd|Court|Ct)\b"
)
_PERSON_CONTEXT = re.compile(r"(?i)\b\w*(?:name|student|customer|employee|patient|owner|person|contact|member|client)\w*\b")
_PERSON_LITERAL = re.compile(r"\"[A-Z][a-z]+ (?:[A-Z]\. )?[A-Z][a-z]+\"")


@dataclass(frozen=True)
class _Rule:
    kind: str
    confidence: str
    detail: str
    pattern: re.Pattern[str]


_RULES: tuple[_Rule, ...] = tuple(
    _Rule(name, "high", "looks like a real secret value", pattern) for name, pattern in _SECRET_PATTERNS
) + (
    _Rule("national_id", "high", "looks like a national identification number", _SSN),
    _Rule("email_address", "medium", "an email address", _EMAIL),
    _Rule("phone_number", "medium", "a phone number", _PHONE),
    _Rule("absolute_path", "medium", "an absolute path, which can reveal a user name",
          re.compile("|".join(p.pattern for p in (_WINDOWS_PATH, _UNC_PATH, _POSIX_HOME)))),
    _Rule("author_identity", "medium", "an author or student name line", _AUTHOR_LINE),
    _Rule("author_identity", "medium", "a name or email comment", _NAME_COMMENT),
    _Rule("identifier", "medium", "looks like a student, employee or patient ID", _STUDENT_ID),
    _Rule("birth_date", "medium", "looks like a date of birth", _BIRTH_DATE),
    _Rule("private_network_address", "medium", "a private network address", _PRIVATE_IP),
    _Rule("street_address", "low", "looks like a street address", _STREET_ADDRESS),
)


def _is_placeholder(value: str) -> bool:
    folded = value.strip().casefold()
    return folded in _PLACEHOLDER_VALUES or folded.startswith(("${", "%", "<")) or set(folded) <= {"*", "x", "."}


def _line_findings(line: str, terms: list[re.Pattern[str]], is_config: bool) -> Iterable[tuple[str, str, str]]:
    for rule in _RULES:
        if rule.pattern.search(line):
            yield rule.kind, rule.confidence, rule.detail
    if any(not _is_placeholder(m.group(1)) for m in _ASSIGNED_LITERAL.finditer(line)):
        yield "hardcoded_credential", "high", "a credential-like name is assigned a literal value"
    if is_config:
        match = _PROPERTY_VALUE.match(line)
        if match and not _is_placeholder(match.group(1)):
            yield "hardcoded_credential", "high", "a credential-like setting has a value"
    if any(is_card_number(m.group(0)) for m in _CARD.finditer(line)):
        yield "payment_card", "high", "looks like a payment card number"
    if _PERSON_LITERAL.search(line) and _PERSON_CONTEXT.search(_PERSON_LITERAL.sub("", line)):
        yield "person_name", "low", "a literal that looks like a person's name, possibly sample data"
    if any(p.search(line) for p in terms):
        yield "local_identity", "high", "contains this computer's user name or one of your private terms"


def scan_text(project_path: str, text: str, identity_terms: Iterable[str] = (), *,
              inspector: "ProjectInspector | None" = None) -> tuple[list[SensitiveFinding], bool]:
    """Findings for one file, and whether the per-file limit cut the list short."""
    terms = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])", re.IGNORECASE) for t in identity_terms]
    suffixes = _CONFIG_SUFFIXES + (inspector.config_file_suffixes if inspector else ())
    is_config = project_path.casefold().endswith(suffixes)
    findings: list[SensitiveFinding] = []
    long_line_noted = False
    for number, line in enumerate(text.splitlines(), start=1):
        if len(line) > MAX_SCANNED_LINE_CHARS:
            if not long_line_noted:
                long_line_noted = True
                findings.append(SensitiveFinding("unscanned_long_line", "low", project_path, number,
                                                 "a very long line that was not scanned; check this file by hand"))
            continue
        seen: set[str] = set()
        extra = inspector.sensitive_rules(line) if inspector else ()
        for kind, confidence, detail in (*_line_findings(line, terms, is_config), *extra):
            if kind in seen:
                continue
            seen.add(kind)
            if len(findings) >= MAX_FINDINGS_PER_FILE:
                return findings, True
            findings.append(SensitiveFinding(kind, confidence, project_path, number, detail))
    return findings, False
