"""Redaction of excerpts before they are stored in an evidence pack.

Removes email addresses, absolute file paths, secret-looking values, author /
student-name comment lines, phone numbers and long literal strings. Each
excerpt records how many of each kind were removed.
"""

from __future__ import annotations

import re
from collections import Counter

from ..devtools.privacy_check import _SECRET_PATTERNS

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
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
_LONG_LITERAL = re.compile(r"(['\"])([^'\"\n]{200,})\1")

PLACEHOLDERS = {
    "email": "[email removed]",
    "path": "[path removed]",
    "secret": "[secret removed]",
    "name": "[name removed]",
    "phone": "[phone removed]",
    "long_literal": "[long text removed]",
}


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
    text = sub(_EMAIL, "email", PLACEHOLDERS["email"], text)
    for pattern in (_WINDOWS_PATH, _UNC_PATH, _POSIX_HOME):
        text = sub(pattern, "path", PLACEHOLDERS["path"], text)
    text = sub(_PHONE, "phone", PLACEHOLDERS["phone"], text)
    text = sub(_LONG_LITERAL, "long_literal", lambda m: m.group(1) + PLACEHOLDERS["long_literal"] + m.group(1), text)
    return text, dict(sorted(counts.items()))
