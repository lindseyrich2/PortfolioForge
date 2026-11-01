"""Turning evidence into confidence. Scores rank candidates; they are not probabilities."""

from __future__ import annotations

import hashlib
import math
from typing import Iterable

from .config import GroupingConfig
from .models import Band, Evidence


def confidence(evidence: Iterable[Evidence], config: GroupingConfig) -> float:
    total = sum(e.weight for e in evidence)
    return 1.0 / (1.0 + math.exp(-(total - config.t("confidence_bias"))))


def band(value: float, config: GroupingConfig) -> Band:
    if value >= config.t("high_band"):
        return Band.HIGH
    if value >= config.t("medium_band"):
        return Band.MEDIUM
    return Band.LOW


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8", "surrogatepass")).hexdigest()
    return f"{prefix}_{digest[:12]}"


def strongest_by_kind(evidence: Iterable[Evidence]) -> list[Evidence]:
    """One item per evidence kind, keeping the strongest, in a stable order."""
    best: dict[str, Evidence] = {}
    for item in evidence:
        current = best.get(item.kind)
        if current is None or abs(item.weight) > abs(current.weight):
            best[item.kind] = item
    return sorted(best.values(), key=lambda e: (-e.weight, e.kind))
