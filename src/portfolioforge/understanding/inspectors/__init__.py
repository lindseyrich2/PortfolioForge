"""Project-type inspectors and the registry that chooses one.

To support another project type, implement :class:`ProjectInspector` and register it. The core
workflow does not change. When no inspector applies, the result is "manual review needed": no
commands and no validation, while the generic privacy scan of approved files still runs.
"""

from __future__ import annotations

from collections.abc import Iterable

from .base import (
    InspectionInput,
    InspectorReport,
    ProjectFacts,
    ProjectInspector,
    SourceFile,
    ValidationSpec,
)
from .java import JavaInspector

MANUAL_REVIEW_REASON = (
    "No inspector supports this project type, so its build and run steps were not determined. "
    "Review them by hand."
)


class InspectorRegistry:
    def __init__(self, inspectors: Iterable[ProjectInspector] = ()) -> None:
        self._inspectors: dict[str, ProjectInspector] = {}
        for inspector in inspectors:
            self.register(inspector)

    def register(self, inspector: ProjectInspector) -> None:
        if inspector.name in self._inspectors:
            raise ValueError("An inspector with this name is already registered.")
        self._inspectors[inspector.name] = inspector

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._inspectors)

    def get(self, name: str | None) -> ProjectInspector | None:
        return self._inspectors.get(name) if name else None

    def select(self, facts: ProjectFacts) -> ProjectInspector | None:
        """The first registered inspector that applies, or None for manual review."""
        return next((i for i in self._inspectors.values() if i.detects(facts)), None)


DEFAULT_REGISTRY = InspectorRegistry([JavaInspector()])

__all__ = [
    "DEFAULT_REGISTRY", "InspectionInput", "InspectorRegistry", "InspectorReport", "JavaInspector",
    "MANUAL_REVIEW_REASON", "ProjectFacts", "ProjectInspector", "SourceFile", "ValidationSpec",
]
