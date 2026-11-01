"""Local storage for grouping results. Always in the user's data folder, never in Git.

Layout (see :mod:`portfolioforge.paths`)::

    <data dir>/groupings/<inventory id>/run.json      latest proposal (regenerated)
    <data dir>/groupings/<inventory id>/report.md     readable report (on request)
    <data dir>/reviews/<inventory id>/decisions.jsonl your corrections (persistent)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..paths import data_dir, ensure_safe_output_dir
from .models import GroupingRun

RUN_FILE = "run.json"
REPORT_FILE = "report.md"


def groupings_dir(inventory_key: str) -> Path:
    if not inventory_key or any(sep in inventory_key for sep in ("/", "\\")) or inventory_key in (".", ".."):
        raise ValueError("Invalid inventory id.")
    return data_dir() / "groupings" / inventory_key


def save_run(run: GroupingRun, inventory_key: str) -> Path:
    folder = ensure_safe_output_dir(groupings_dir(inventory_key))
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / RUN_FILE
    temporary = folder / (RUN_FILE + ".tmp")
    temporary.write_text(run.to_json(), encoding="utf-8")
    os.replace(temporary, target)
    return target


def load_run(inventory_key: str) -> GroupingRun | None:
    path = groupings_dir(inventory_key) / RUN_FILE
    if not path.is_file():
        return None
    return GroupingRun.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_report(text: str, inventory_key: str) -> Path:
    folder = ensure_safe_output_dir(groupings_dir(inventory_key))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / REPORT_FILE
    path.write_text(text, encoding="utf-8")
    return path
