"""Local evidence extraction from approved files. Nothing is ever executed.

Python is parsed with the standard ``ast`` module (parsing never runs code).
Notebooks are parsed as JSON; cell outputs are excluded. Java, R, R Markdown
and SQL use line-based patterns. Every evidence item keeps its file and line
(or notebook cell) reference and a hash of its redacted text, so later AI
claims can be checked against exactly what was extracted.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterable

from ..grouping.scoring import stable_id
from .models import EvidenceItem, EvidencePack, FileApprovalRecord, Technology
from .reader import ApprovedFileReader, ReadRefused
from .redact import redact

MAX_EXCERPT_CHARS = 1500
MAX_ITEMS_PER_FILE = 200


@dataclass(frozen=True)
class RawEvidence:
    kind: str
    line_start: int
    line_end: int
    text: str
    cell: int | None = None
    module: str | None = None
    """Imported module or package, for technology detection."""


# --- technology detection (from imports and declared dependencies only) ------------

PYTHON_TECH = {
    "numpy": "NumPy", "pandas": "pandas", "sklearn": "scikit-learn", "scikit-learn": "scikit-learn",
    "torch": "PyTorch", "tensorflow": "TensorFlow", "keras": "Keras", "matplotlib": "Matplotlib",
    "seaborn": "seaborn", "scipy": "SciPy", "statsmodels": "statsmodels", "plotly": "Plotly",
    "flask": "Flask", "django": "Django", "fastapi": "FastAPI", "requests": "Requests",
    "bs4": "Beautiful Soup", "beautifulsoup4": "Beautiful Soup", "nltk": "NLTK", "spacy": "spaCy",
    "transformers": "Hugging Face Transformers", "cv2": "OpenCV", "opencv-python": "OpenCV",
    "sqlalchemy": "SQLAlchemy", "sqlite3": "SQLite", "pyspark": "Apache Spark", "dask": "Dask",
    "xgboost": "XGBoost", "lightgbm": "LightGBM", "networkx": "NetworkX", "pytest": "pytest",
    "streamlit": "Streamlit", "boto3": "AWS SDK for Python (boto3)", "tkinter": "Tkinter", "pygame": "pygame",
}
R_TECH = {
    "ggplot2": "ggplot2", "dplyr": "dplyr", "tidyr": "tidyr", "tidyverse": "tidyverse", "caret": "caret",
    "randomforest": "randomForest", "data.table": "data.table", "shiny": "Shiny", "lme4": "lme4",
    "glmnet": "glmnet", "xgboost": "XGBoost", "readr": "readr", "stringr": "stringr", "knitr": "knitr",
    "rmarkdown": "R Markdown", "tidymodels": "tidymodels", "forecast": "forecast",
}
JAVA_TECH = {
    "javax.swing": "Swing", "java.awt": "AWT", "javafx": "JavaFX", "java.sql": "JDBC", "org.junit": "JUnit",
    "org.springframework": "Spring", "java.net.http": "Java HTTP Client", "com.google.gson": "Gson",
}
LANGUAGE_TECH = {"Python": "Python", "Jupyter Notebook": "Jupyter Notebook", "Java": "Java", "R": "R",
                 "R Markdown": "R Markdown", "SQL": "SQL"}


def _tech_for(language: str, module: str) -> str | None:
    if language in ("Python", "Jupyter Notebook", "requirements"):
        return PYTHON_TECH.get(module.split(".")[0].casefold()) or PYTHON_TECH.get(module.casefold())
    if language in ("R", "R Markdown"):
        return R_TECH.get(module.casefold())
    if language == "Java":
        return next((t for prefix, t in JAVA_TECH.items() if module.startswith(prefix)), None)
    return None


# --- Python -----------------------------------------------------------------------


def python_evidence(source: str, cell: int | None = None) -> list[RawEvidence]:
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return _python_fallback(lines, cell)
    out: list[RawEvidence] = []
    doc = ast.get_docstring(tree)
    if doc:
        out.append(RawEvidence("docstring", 1, max(1, doc.count("\n") + 1), doc, cell))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(RawEvidence("import", node.lineno, node.lineno, lines[node.lineno - 1].strip(), cell,
                                       alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(RawEvidence("import", node.lineno, node.lineno, lines[node.lineno - 1].strip(), cell,
                                   node.module))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            header = lines[node.lineno - 1].strip()
            docstring = ast.get_docstring(node)
            text = header + (f"\n    \"\"\"{docstring}\"\"\"" if docstring else "")
            out.append(RawEvidence("definition", node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno,
                                   text, cell))
    header = _leading_comments(lines, "#")
    if header:
        out.append(RawEvidence("comment", 1, header[0], header[1], cell))
    return sorted(out, key=lambda e: (e.line_start, e.kind))


def _python_fallback(lines: list[str], cell: int | None) -> list[RawEvidence]:
    out = []
    pattern = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))")
    for n, line in enumerate(lines, start=1):
        m = pattern.match(line)
        if m:
            out.append(RawEvidence("import", n, n, line.strip(), cell, m.group(1) or m.group(2)))
        elif re.match(r"^\s*(?:async\s+)?(?:def|class)\s+\w+", line):
            out.append(RawEvidence("definition", n, n, line.strip(), cell))
    return out


def _leading_comments(lines: list[str], prefix: str, limit: int = 15) -> tuple[int, str] | None:
    block = []
    for line in lines[:limit]:
        stripped = line.strip()
        if stripped.startswith(prefix) and not stripped.startswith("#!"):
            block.append(stripped)
        elif stripped or block:
            break
    return (len(block), "\n".join(block)) if block else None


# --- notebooks -----------------------------------------------------------------------


def notebook_evidence(text: str) -> tuple[list[RawEvidence], list[str]]:
    try:
        data = json.loads(text)
        cells = data.get("cells", [])
    except (ValueError, AttributeError):
        return [], ["a notebook could not be parsed as JSON"]
    out: list[RawEvidence] = []
    outputs = 0
    for number, cell in enumerate(cells, start=1):
        source = cell.get("source", "")
        source = "".join(source) if isinstance(source, list) else str(source)
        if cell.get("cell_type") == "markdown" and source.strip():
            out.append(RawEvidence("markdown", 1, max(1, source.count("\n") + 1), source.strip(), number))
        elif cell.get("cell_type") == "code":
            if cell.get("outputs"):
                outputs += 1
            cleaned = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith(("%", "!")))
            out.extend(python_evidence(cleaned, number))
    warnings = [f"{outputs} notebook cell output(s) excluded"] if outputs else []
    return out, warnings


# --- Java, R, SQL, supporting files -------------------------------------------------

_JAVA_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\.\*)?\s*;")
_JAVA_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;")
_JAVA_TYPE = re.compile(r"^\s*(?:(?:public|protected|private|abstract|final|static|sealed)\s+)*"
                        r"(class|interface|enum|record)\s+(\w+)")
_JAVA_METHOD = re.compile(r"^\s*(?:(?:public|protected|private|static|final|synchronized|abstract)\s+)+"
                          r"[\w<>\[\],.\s]+?\s+(\w+)\s*\([^;{]*\)\s*(?:throws\s+[\w.,\s]+)?\{?\s*$")


def java_evidence(source: str) -> list[RawEvidence]:
    out = []
    for n, line in enumerate(source.splitlines(), start=1):
        if m := _JAVA_IMPORT.match(line):
            out.append(RawEvidence("import", n, n, line.strip(), module=m.group(1)))
        elif _JAVA_PACKAGE.match(line):
            out.append(RawEvidence("statement", n, n, line.strip()))
        elif _JAVA_TYPE.match(line) or _JAVA_METHOD.match(line):
            out.append(RawEvidence("definition", n, n, line.strip()))
    header = _java_header(source)
    if header:
        out.append(RawEvidence("comment", 1, header[0], header[1]))
    return out


def _java_header(source: str) -> tuple[int, str] | None:
    m = re.match(r"\s*(/\*.*?\*/)", source, re.DOTALL)
    return (m.group(1).count("\n") + 1, m.group(1)) if m else None


_R_LIBRARY = re.compile(r"\b(?:library|require|requireNamespace)\(\s*['\"]?([\w.]+)['\"]?")
_R_NAMESPACE = re.compile(r"\b([A-Za-z][\w.]*)::\w+")
_R_FUNCTION = re.compile(r"^\s*([\w.]+)\s*(?:<-|=)\s*function\s*\(")


def r_evidence(source: str, markdown: bool = False) -> list[RawEvidence]:
    out: list[RawEvidence] = []
    in_chunk = not markdown
    for n, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if markdown and stripped.startswith("```"):
            if re.match(r"```\s*\{r", stripped):
                in_chunk = True
                out.append(RawEvidence("chunk", n, n, stripped))
            else:
                in_chunk = False
            continue
        if markdown and not in_chunk:
            if re.match(r"#{1,6}\s+\S", stripped):
                out.append(RawEvidence("markdown", n, n, stripped))
            continue
        for m in _R_LIBRARY.finditer(line):
            out.append(RawEvidence("import", n, n, stripped, module=m.group(1)))
        for m in _R_NAMESPACE.finditer(line):
            out.append(RawEvidence("import", n, n, stripped, module=m.group(1)))
        if _R_FUNCTION.match(line):
            out.append(RawEvidence("definition", n, n, stripped))
        elif stripped.startswith("#'"):
            out.append(RawEvidence("docstring", n, n, stripped))
    return out


_SQL_STATEMENT = re.compile(r"^\s*(create\s+(?:or\s+replace\s+)?(?:table|view|index|procedure|function)|select|"
                            r"insert\s+into|update|delete\s+from|with)\b", re.IGNORECASE)
_SQL_TABLE = re.compile(r"\b(?:from|join|into|update|table|view)\s+([\w.\"`\[\]]+)", re.IGNORECASE)


def sql_evidence(source: str) -> list[RawEvidence]:
    out = []
    for n, line in enumerate(source.splitlines(), start=1):
        if _SQL_STATEMENT.match(line):
            tables = sorted({t.strip('"`[]') for t in _SQL_TABLE.findall(line)})
            suffix = f"    -- tables: {', '.join(tables)}" if tables else ""
            out.append(RawEvidence("statement", n, n, line.strip() + suffix))
    return out


def readme_evidence(source: str) -> list[RawEvidence]:
    out, paragraph, start = [], [], None
    for n, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if re.match(r"#{1,6}\s+\S", stripped) or (stripped and set(stripped) <= {"=", "-"} and n > 1):
            out.append(RawEvidence("readme", n, n, stripped))
        elif stripped and len(out) <= 3 and len(" ".join(paragraph)) < 800:
            paragraph.append(stripped)
            start = start or n
    if paragraph:
        out.append(RawEvidence("readme", start or 1, (start or 1) + len(paragraph) - 1, " ".join(paragraph)))
    return out


def requirements_evidence(source: str) -> list[RawEvidence]:
    out = []
    for n, line in enumerate(source.splitlines(), start=1):
        stripped = line.split("#", 1)[0].strip()
        if stripped and not stripped.startswith("-"):
            package = re.split(r"[\s<>=!~;\[]", stripped, maxsplit=1)[0]
            out.append(RawEvidence("dependency", n, n, stripped, module=package))
    return out


def raw_evidence_for(language: str, text: str) -> tuple[list[RawEvidence], list[str]]:
    if language == "Python":
        return python_evidence(text), []
    if language == "Jupyter Notebook":
        return notebook_evidence(text)
    if language == "Java":
        return java_evidence(text), []
    if language in ("R", "R Markdown"):
        return r_evidence(text, markdown=language == "R Markdown"), []
    if language == "SQL":
        return sql_evidence(text), []
    if language == "README":
        return readme_evidence(text), []
    if language == "requirements":
        return requirements_evidence(text), []
    return [], [f"no extractor for {language}"]


# --- evidence packs ----------------------------------------------------------------------


def build_evidence_pack(approval: FileApprovalRecord, reader: ApprovedFileReader,
                        now: datetime | None = None) -> EvidencePack:
    """Read each approved file once and extract redacted, referenced evidence."""
    items: list[EvidenceItem] = []
    files: list[dict] = []
    warnings: list[str] = []
    tech_refs: dict[str, list[str]] = {}
    for approved in approval.files:
        try:
            text = reader.read_text(approved.file_id)
        except ReadRefused as exc:
            files.append({"project_path": approved.project_path, "language": approved.language,
                          "evidence_items": 0, "status": f"not read: {exc}"})
            continue
        raw, file_warnings = raw_evidence_for(approved.language, text)
        warnings.extend(f"{approved.project_path}: {w}" for w in file_warnings)
        if len(raw) > MAX_ITEMS_PER_FILE:
            warnings.append(f"{approved.project_path}: kept the first {MAX_ITEMS_PER_FILE} of {len(raw)} items")
            raw = raw[:MAX_ITEMS_PER_FILE]
        count = 0
        for r in raw:
            excerpt = r.text if len(r.text) <= MAX_EXCERPT_CHARS else r.text[:MAX_EXCERPT_CHARS] + " [truncated]"
            clean, redactions = redact(excerpt)
            item_id = stable_id("e", approved.file_id, r.kind, str(r.cell or 0), str(r.line_start), str(count))
            items.append(EvidenceItem(
                id=item_id, file_id=approved.file_id, project_path=approved.project_path,
                language=approved.language, kind=r.kind, line_start=r.line_start, line_end=r.line_end, cell=r.cell,
                text=clean, sha256=hashlib.sha256(clean.encode("utf-8")).hexdigest(), redactions=redactions,
            ))
            if r.module:
                tech = _tech_for(approved.language, r.module)
                if tech:
                    tech_refs.setdefault(tech, []).append(item_id)
            count += 1
        language_tech = LANGUAGE_TECH.get(approved.language)
        if language_tech and count:
            tech_refs.setdefault(language_tech, []).append(items[-1].id)
        files.append({"project_path": approved.project_path, "language": approved.language,
                      "evidence_items": count, "status": "read"})
    created = now or datetime.now(UTC)
    return EvidencePack(
        id=stable_id("p", approval.id, created.isoformat()), inventory_key=approval.inventory_key,
        group_id=approval.group_id, approval_id=approval.id, created_at=created, items=tuple(items),
        technologies=tuple(Technology(name, tuple(dict.fromkeys(refs))) for name, refs in sorted(tech_refs.items())),
        files=tuple(files), warnings=tuple(warnings),
    )


def evidence_summary(pack: EvidencePack) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in pack.items:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    return dict(sorted(counts.items()))


