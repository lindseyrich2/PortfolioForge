"""Inventory schema v2: project markers and per-folder unrecognized counts."""

from __future__ import annotations

import pytest

from conftest import make_tree
from portfolioforge.inventory import Inventory, rules


@pytest.fixture
def project(tmp_path):
    return make_tree(
        tmp_path / "Course",
        {
            "proj/README.md": "",
            "proj/requirements.txt": "",
            "proj/pyproject.toml": "",
            "proj/Makefile": "",
            "proj/DESCRIPTION": "",
            "proj/analysis.Rproj": "",
            "proj/.gitignore": "",
            "proj/.git/HEAD": "",
            "proj/.idea/workspace.xml": "",
            "proj/nbproject/project.xml": "",
            "proj/main.py": "",
            "proj/notes.weird": "",
            "proj/secret-plans.xyz": "",
            "proj/credentials.json": "",
            "Taxes/.git/HEAD": "",
            "Taxes/README.md": "",
        },
    )


def _dirs(inventory):
    return {d.relative_path: d for d in inventory.directories}


def test_marker_files_and_dirs_are_recorded_from_the_fixed_list(scan, project):
    proj = _dirs(scan(project))["proj"]
    assert proj.marker_files == (
        ".gitignore", "analysis.Rproj", "DESCRIPTION", "Makefile",
        "pyproject.toml", "README.md", "requirements.txt",
    )
    assert proj.marker_dirs == (".git", ".idea", "nbproject")


def test_unrecognized_files_are_counted_per_folder_not_named(scan, project):
    inventory = scan(project)
    proj = _dirs(inventory)["proj"]
    assert proj.unrecognized_extensions == {"(none)": 2, ".rproj": 1, ".toml": 1, ".weird": 1, ".xyz": 1}
    exported = inventory.to_json()
    assert "secret-plans" not in exported and "notes.weird" not in exported


def test_sensitive_and_private_names_are_never_markers(scan, project):
    inventory = scan(project)
    dirs = _dirs(inventory)
    assert "credentials.json" not in dirs["proj"].marker_files
    assert "Taxes" not in dirs  # private folder: never listed, so no markers
    assert rules.is_project_marker_file("credentials.json") is False


def test_new_inventories_use_schema_v2(scan, project):
    assert scan(project).report.schema_version == 2


def test_schema_v1_inventories_still_load(scan, project):
    data = scan(project).to_dict()
    data["report"]["schema_version"] = 1
    for directory in data["directories"]:
        for key in ("marker_files", "marker_dirs", "unrecognized_extensions"):
            directory.pop(key)
    loaded = Inventory.from_dict(data)
    assert loaded.report.schema_version == 1
    assert all(d.marker_files == () and d.unrecognized_extensions == {} for d in loaded.directories)


def test_unknown_schema_versions_are_rejected(scan, project):
    data = scan(project).to_dict()
    data["report"]["schema_version"] = 99
    with pytest.raises(ValueError):
        Inventory.from_dict(data)


@pytest.mark.parametrize("name", ["README", "readme.md", "Pom.XML", "CMakeLists.txt", "My App.sln", "x.RPROJ"])
def test_marker_names_match_case_insensitively(name):
    assert rules.is_project_marker_file(name)


@pytest.mark.parametrize("name", ["report.docx", "secret-plans.xyz", "notes.txt", "data.csv"])
def test_other_names_are_not_markers(name):
    assert not rules.is_project_marker_file(name)
