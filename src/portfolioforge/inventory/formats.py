"""File format registry.

Maps lowercase file extensions to an inventory category and, where it can be
inferred from the extension alone, a programming language. Nothing here ever
looks at file contents.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class Category(StrEnum):
    """Broad kind of file, used by later grouping and analysis stages."""

    CODE = "code"
    NOTEBOOK = "notebook"
    DOCUMENT = "document"
    PRESENTATION = "presentation"
    SPREADSHEET = "spreadsheet"
    DATA = "data"
    ARCHIVE = "archive"
    IMAGE = "image"


@dataclass(frozen=True, slots=True)
class FileFormat:
    category: Category
    language: str | None = None


_CODE_LANGUAGES: dict[str, str] = {
    ".py": "Python",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".sql": "SQL",
    ".r": "R",
    ".rmd": "R Markdown",
    ".qmd": "Quarto",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".hpp": "C++",
    ".cs": "C#",
    ".go": "Go",
    ".rs": "Rust",
    ".scala": "Scala",
    ".kt": "Kotlin",
    ".swift": "Swift",
    ".m": "MATLAB",
    ".jl": "Julia",
    ".sas": "SAS",
    ".do": "Stata",
    ".sh": "Shell",
    ".ps1": "PowerShell",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".php": "PHP",
    ".rb": "Ruby",
    ".hs": "Haskell",
    ".asm": "Assembly",
    ".s": "Assembly",
    ".tex": "LaTeX",
}

_OTHER_CATEGORIES: dict[Category, tuple[str, ...]] = {
    Category.DOCUMENT: (".pdf", ".docx", ".doc", ".odt", ".rtf", ".md", ".txt", ".bib"),
    # Keynote's ".key" is deliberately absent: it collides with private-key
    # files, which are treated as sensitive and skipped.
    Category.PRESENTATION: (".pptx", ".ppt", ".odp"),
    Category.SPREADSHEET: (".xlsx", ".xls", ".ods"),
    Category.DATA: (
        ".csv", ".tsv", ".json", ".xml", ".parquet", ".arff", ".mat", ".sav",
        ".dta", ".h5", ".hdf5", ".pkl", ".pickle", ".npy", ".npz", ".pt",
        ".pth", ".joblib",
    ),
    Category.ARCHIVE: (".zip", ".tar", ".gz", ".tgz", ".7z", ".rar"),
}

IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".tif", ".tiff", ".webp"}
)


def _build_registry() -> Mapping[str, FileFormat]:
    registry: dict[str, FileFormat] = {
        ext: FileFormat(Category.CODE, lang) for ext, lang in _CODE_LANGUAGES.items()
    }
    registry[".ipynb"] = FileFormat(Category.NOTEBOOK, "Jupyter Notebook")
    for category, extensions in _OTHER_CATEGORIES.items():
        for ext in extensions:
            registry[ext] = FileFormat(category)
    for ext in IMAGE_EXTENSIONS:
        registry[ext] = FileFormat(Category.IMAGE)
    return MappingProxyType(registry)


FORMATS: Mapping[str, FileFormat] = _build_registry()


def lookup(extension: str) -> FileFormat | None:
    """Return the format for an extension such as ``".py"`` (case-insensitive)."""
    return FORMATS.get(extension.lower())
