"""Redaction of excerpts before they are stored in an evidence pack.

Removes email addresses, absolute file paths, secret-looking values, author /
student-name comment lines, national ID and payment card numbers, phone numbers
and long literal strings. Each excerpt records how many of each kind were removed.
"""

from __future__ import annotations

import re
from collections import Counter

from ..devtools.privacy_check import _SECRET_PATTERNS

# Unquoted environment-variable style assignments (API_TOKEN=abc123) in commands and .env text.
# Upper-case names and no spaces only, so code such as `token = tokenizer.tokenize(x)` is kept.
_UNQUOTED_SECRET = re.compile(
    r"\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*)(=)(?!['\"\[$%{])([^\s'\"]{6,})"
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_WINDOWS_PATH = re.compile(r"(?<![\w])[A-Za-z]:[\\/][^\s\"'<>|*?`,;)]+")
_UNC_PATH = re.compile(r"\\\\[^\s\"'<>|*?`]+")
_POSIX_HOME = re.compile(r"(?<![\w.])/(?:home|Users|mnt|media|Volumes)/[^\s\"'<>`,;)]+")
_ASSIGNED_SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|apikey|access[_-]?key|token|auth)\b(\s*[:=]\s*)(['\"])[^'\"]+\3"
)
# Authorship lines anywhere (including docstrings): "Author: ...", "@author ...", "Created by ...".
_AUTHOR_LINE = re.compile(
    r"(?im)^(\s*(?:#'|#+|//+|/?\*+|--|%+|<!--)?\s*@?(?:author|authors|created\s+by|written\s+by|"
    r"submitted\s+by|student\s*(?:id|number)|netid)\b\s*[:=-]?\s*)(\S.*)$"
)
# "Name:", "Student:" and "Email:" only inside comments, so code such as `name = input()` is kept.
_NAME_COMMENT = re.compile(
    r"(?im)^(\s*(?:#'|#+|//+|/?\*+|--|%+|<!--)\s*(?:name|student|email)\s*[:=-]\s*)(\S.*)$"
)
# Shared with the sensitive-data scan, so what it flags and what is removed can't drift apart.
_SSN = re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])")
_CARD = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
_LONG_LITERAL = re.compile(r"(['\"])([^'\"\n]{200,})\1")

PLACEHOLDERS = {
    "email": "[email removed]",
    "path": "[path removed]",
    "secret": "[secret removed]",
    "name": "[name removed]",
    "national_id": "[id number removed]",
    "payment_card": "[card number removed]",
    "phone": "[phone removed]",
    "long_literal": "[long text removed]",
}


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def is_card_number(text: str) -> bool:
    """Passes the card checksum, which most long numbers (IDs, timestamps, measurements) do not."""
    return _luhn(re.sub(r"\D", "", text))


def redact(text: str) -> tuple[str, dict[str, int]]:
    counts: Counter[str] = Counter()

    def sub(pattern: re.Pattern[str], kind: str, replacement, value: str) -> str:
        result, n = pattern.subn(replacement, value)
        if n:
            counts[kind] += n
        return result

    text = sub(_AUTHOR_LINE, "name", lambda m: m.group(1) + PLACEHOLDERS["name"], text)
    text = sub(_NAME_COMMENT, "name", lambda m: m.group(1) + PLACEHOLDERS["name"], text)
    for _, pattern in _SECRET_PATTERNS:
        text = sub(pattern, "secret", PLACEHOLDERS["secret"], text)
    text = sub(_ASSIGNED_SECRET, "secret", lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{PLACEHOLDERS['secret']}"
               f"{m.group(3)}", text)
    text = sub(_UNQUOTED_SECRET, "secret", lambda m: f"{m.group(1)}{m.group(2)}{PLACEHOLDERS['secret']}", text)
    text = sub(_EMAIL, "email", PLACEHOLDERS["email"], text)
    for pattern in (_WINDOWS_PATH, _UNC_PATH, _POSIX_HOME):
        text = sub(pattern, "path", PLACEHOLDERS["path"], text)
    text = sub(_SSN, "national_id", PLACEHOLDERS["national_id"], text)

    def card(match: re.Match[str]) -> str:
        if not is_card_number(match.group(0)):
            return match.group(0)
        counts["payment_card"] += 1
        return PLACEHOLDERS["payment_card"]

    text = _CARD.sub(card, text)
    text = sub(_PHONE, "phone", PLACEHOLDERS["phone"], text)
    text = sub(_LONG_LITERAL, "long_literal", lambda m: m.group(1) + PLACEHOLDERS["long_literal"] + m.group(1), text)
    return text, dict(sorted(counts.items()))
