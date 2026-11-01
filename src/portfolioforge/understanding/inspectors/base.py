"""The interface a project-type inspector implements.

The core workflow (approvals, reading files, the privacy scan, validation gates, the README)
knows nothing about any language. An inspector supplies what is specific to one project type:
which build files exist, how to read them, which commands they support, which tools may run,
and which extra paths, values and sensitive-data patterns matter.

An inspector never opens a file or starts a process. The core reads only approved files, hands
it their text, and is the only code that runs the commands it describes. Its answers about paths
and commands are checked again by the core, so a faulty inspector cannot widen what is allowed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from ..models import (
    BuildInspection,
    CommandFinding,
    EntryPoint,
    PackageInfo,
    RiskSignal,
    SourceRef,
    ToolRequirement,
)


@dataclass(frozen=True)
class ProjectFacts:
    """Metadata only, for deciding whether an inspector applies. Nothing has been read."""

    member_paths: tuple[str, ...]
    marker_files: frozenset[str]
    marker_dirs: frozenset[str]


@dataclass(frozen=True)
class SourceFile:
    file_id: str
    project_path: str
    language: str
    text: str


@dataclass(frozen=True)
class InspectionInput:
    build_files: Mapping[str, str]
    """Project path -> text, for approved build files that could be read."""
    sources: tuple[SourceFile, ...]
    """Source files approved at Gate 1 that could be read."""
    unapproved_paths: tuple[str, ...]
    """Project files that were not read."""
    present: tuple[str, ...]
    """Which of the inspector's presence checks exist. Seen by name only."""


@dataclass
class InspectorReport:
    build_systems: list[str] = field(default_factory=list)
    entry_points: list[EntryPoint] = field(default_factory=list)
    packages: list[PackageInfo] = field(default_factory=list)
    source_roots: list[str] = field(default_factory=list)
    test_roots: list[str] = field(default_factory=list)
    requirements: list[ToolRequirement] = field(default_factory=list)
    dependencies: list[SourceRef] = field(default_factory=list)
    commands: list[CommandFinding] = field(default_factory=list)
    risks: list[RiskSignal] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    options: dict[str, str] = field(default_factory=dict)
    """Checked values the inspector needs again to build validation commands."""


@dataclass(frozen=True)
class ValidationSpec:
    files: tuple[str, ...]
    """Project paths to copy into the temporary folder."""
    entry_point: str | None
    options: dict[str, str]
    warnings: tuple[str, ...]


class ProjectInspector(ABC):
    name: str
    #: Path relative to a build root -> role. The whole of what can be offered for approval.
    build_files: Mapping[str, str] = {}
    #: Files that often hold credentials. Their presence is reported; they are never read.
    review_only_files: tuple[str, ...] = ()
    #: Names on the inventory's marker list that show a folder is a build root (casefolded).
    root_marker_files: frozenset[str] = frozenset()
    root_marker_dirs: frozenset[str] = frozenset()
    #: Files whose existence matters, checked by name only.
    presence_checks: tuple[str, ...] = ()
    #: Extra file suffixes whose credential-like settings are flagged even when unquoted.
    config_file_suffixes: tuple[str, ...] = ()
    #: The only programs a validation plan may start.
    allowed_tools: frozenset[str] = frozenset()
    #: Folders to create in the temporary copy.
    scratch_dirs: tuple[str, ...] = ()

    @abstractmethod
    def detects(self, facts: ProjectFacts) -> bool:
        """Whether this inspector applies. Metadata only."""

    @abstractmethod
    def inspect(self, inputs: InspectionInput) -> InspectorReport:
        """Work out how to build and run from text already read. Must not touch the disk."""

    def sensitive_rules(self, line: str) -> Iterable[tuple[str, str, str]]:
        """Extra (kind, confidence, detail) findings for one line. Details never quote the line."""
        return ()

    # --- optional validation. By default an inspector offers none ------------------------------

    def validation_actions(self) -> tuple[str, ...]:
        return ()

    def runs_project_code(self, action: str) -> bool:
        """Whether this action runs the project's code. Assumed so unless an inspector says not."""
        return True

    def needs_entry_point(self, action: str) -> bool:
        return False

    def is_validation_source(self, language: str, project_path: str) -> bool:
        """Whether an approved file is a candidate for the temporary copy."""
        return False

    def prepare_validation(self, action: str, sources: tuple[SourceFile, ...], inspection: BuildInspection,
                           entry_point: str | None) -> ValidationSpec:
        raise NotImplementedError

    def build_commands(self, action: str, files: tuple[str, ...], entry_point: str | None,
                       options: Mapping[str, str]) -> tuple[tuple[str, ...], ...]:
        """The exact commands for a plan. Must be deterministic: they are rebuilt and compared."""
        raise NotImplementedError

    def stage_of(self, command: tuple[str, ...]) -> str:
        """``compile`` (a failure stops here) or ``run``."""
        return "run"

    def is_safe_copy_path(self, project_path: str) -> bool:
        """Extra path checks, on top of the core's own."""
        return True

    def is_safe_entry_point(self, entry_point: str) -> bool:
        return False

    def options_are_safe(self, options: Mapping[str, str]) -> bool:
        return not options

    def version_args(self, tool: str) -> tuple[str, ...]:
        """Arguments that make a tool print its version; empty to skip."""
        return ()

    def failure_hints(self, stderr: str) -> Iterable[str]:
        return ()
