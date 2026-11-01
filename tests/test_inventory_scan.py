"""Inventory contents for realistic academic folders (targeted scans)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import EXPECTED_ACADEMIC_FILES, file_paths, make_tree, skipped_pairs
from portfolioforge.inventory import (
    Category,
    Inventory,
    InventoryConfig,
    JsonlSink,
    MemorySink,
    load_inventory,
    lookup,
    plan_targeted,
    run_scan,
)


def test_every_relevant_file_is_inventoried(scan, college):
    inventory = scan(college)
    assert file_paths(inventory) == EXPECTED_ACADEMIC_FILES
    assert inventory.complete


def test_multiple_assignments_in_one_folder_are_separate_records(scan, college):
    by_path = {f.relative_path: f for f in scan(college).files}
    assert by_path["CS101 Intro/hw1.py"].parent_dir == "CS101 Intro"
    assert by_path["CS101 Intro/hw2.py"].parent_dir == "CS101 Intro"
    assert by_path["CS101 Intro/hw3/main.py"].parent_dir == "CS101 Intro/hw3"
    assert len({by_path[p].id for p in ("CS101 Intro/hw1.py", "CS101 Intro/hw2.py")}) == 2


def test_generic_notebook_names_get_distinct_records(scan, college):
    notebooks = [f for f in scan(college).files if f.stem.startswith("Untitled")]
    assert sorted(f.relative_path for f in notebooks) == [
        "CS229 ML/Assignment 2/Untitled.ipynb",
        "CS229 ML/Untitled.ipynb",
        "CS229 ML/Untitled1.ipynb",
    ]
    assert len({f.id for f in notebooks}) == 3


def test_standalone_files_need_no_readme_or_config(scan, tmp_path):
    folder = make_tree(tmp_path / "Downloads", {"Untitled.ipynb": "{}", "main.py": ""})
    assert file_paths(scan(folder)) == {"Untitled.ipynb", "main.py"}


def test_extensions_are_case_insensitive_and_names_preserved(scan, college):
    by_path = {f.relative_path: f for f in scan(college).files}
    upper, lower = by_path["Stats/analysis.R"], by_path["Stats/helper.r"]
    assert (upper.extension, upper.language) == (".r", "R")
    assert (lower.extension, lower.language) == (".r", "R")
    assert upper.name == "analysis.R"
    assert by_path["Databases/schema.SQL"].extension == ".sql"


def test_categories_and_languages(scan, college):
    by_path = {f.relative_path: f for f in scan(college).files}
    expected = {
        "CS101 Intro/hw1.py": (Category.CODE, "Python"),
        "CS229 ML/Untitled.ipynb": (Category.NOTEBOOK, "Jupyter Notebook"),
        "Java Course/src/Main.java": (Category.CODE, "Java"),
        "Databases/queries.sql": (Category.CODE, "SQL"),
        "CS101 Intro/Lab Reports/Lab 1.docx": (Category.DOCUMENT, None),
        "CS229 ML/final_project/Final Report.pdf": (Category.DOCUMENT, None),
        "CS229 ML/final_project/slides.pptx": (Category.PRESENTATION, None),
        "Databases/grades.xlsx": (Category.SPREADSHEET, None),
        "CS229 ML/final_project/data/train.csv": (Category.DATA, None),
        "Archive/old_hw.zip": (Category.ARCHIVE, None),
    }
    for path, (category, language) in expected.items():
        assert (by_path[path].category, by_path[path].language) == (category, language), path


@pytest.mark.parametrize(
    "extension", [".py", ".ipynb", ".java", ".js", ".sql", ".r", ".cpp", ".c", ".pdf",
                  ".docx", ".pptx", ".csv", ".xlsx", ".m", ".rmd", ".tex"],
)
def test_registry_covers_academic_formats(extension):
    assert lookup(extension) is not None
    assert lookup(extension.upper()) == lookup(extension)


def test_folder_structure_and_metadata(scan, college):
    target = college / "CS101 Intro" / "hw3" / "main.py"
    stamp = datetime(2021, 3, 14, 15, 9, 26, tzinfo=UTC).timestamp()
    os.utime(target, (stamp, stamp))
    by_path = {f.relative_path: f for f in scan(college).files}

    record = by_path["CS101 Intro/hw3/main.py"]
    assert record.name == "main.py"
    assert record.stem == "main"
    assert record.path_parts == ("CS101 Intro", "hw3")
    assert record.depth == 2
    assert record.size_bytes == target.stat().st_size
    assert record.modified_at == datetime(2021, 3, 14, 15, 9, 26, tzinfo=UTC)
    assert Path(record.absolute_path) == target
    assert record.location_hint == str(college)

    loose = by_path["notebook.ipynb"]
    assert (loose.parent_dir, loose.path_parts, loose.depth) == ("", (), 0)
    assert "Stats/Résumé notes.txt" in by_path and "Stats/数据分析.csv" in by_path


def test_directory_records_describe_course_structure(scan, college):
    dirs = {d.relative_path: d for d in scan(college).directories}
    root = dirs[""]
    assert (root.name, root.parent_dir, root.depth, root.file_count) == ("College", None, 0, 1)
    assert root.subdirectory_count == 10  # 9 course/misc folders + Empty Folder

    ml = dirs["CS229 ML"]
    assert (ml.file_count, ml.subdirectory_count) == (2, 2)  # checkpoints folder not counted
    assert ml.categories == {"notebook": 2}
    assert dirs["Empty Folder"].file_count == 0
    assert "venv" not in dirs and "my_env" not in dirs and "CS229 ML/.ipynb_checkpoints" not in dirs


def test_ignored_locations_are_recorded_with_reasons(scan, college):
    skipped = skipped_pairs(scan(college))
    expected = {
        ("CS101 Intro/__pycache__", "ignored_name"),
        ("CS229 ML/.ipynb_checkpoints", "hidden"),
        ("Java Course/.idea", "hidden"),
        ("Java Course/out", "build_dir"),
        ("Java Course/bin", "build_dir"),
        ("venv", "virtual_env"),
        ("my_env", "virtual_env"),
        ("conda_env", "virtual_env"),
        ("node_modules", "ignored_name"),
        ("__MACOSX", "ignored_name"),
        ("CS101 Intro/Lab Reports/~$Lab 1.docx", "office_lock_file"),
        ("Thumbs.db", "os_metadata_file"),
        ("desktop.ini", "os_metadata_file"),
    }
    assert expected <= skipped


def test_folder_named_env_is_not_mistaken_for_a_virtual_env(scan, college):
    assert "RL Project/env/gridworld.py" in file_paths(scan(college))


def test_sensitive_files_are_skipped_and_their_names_redacted(scan, college):
    inventory = scan(college)
    sensitive = [s for s in inventory.skipped if s.reason.value == "sensitive_name"]
    assert sorted(s.relative_path for s in sensitive) == [
        "CS229 ML/final_project/[redacted]",
        "CS229 ML/final_project/[redacted]",
        "keys/[redacted]",
        "keys/[redacted]",
    ]
    exported = inventory.to_json()
    for name in ("credentials.json", "id_rsa", "server.pem", "API_KEY", '.env"'):
        assert name not in exported


def test_unrecognized_extensions_are_counted_not_recorded(scan, college):
    inventory = scan(college)
    assert inventory.report.unrecognized_extensions == {"(none)": 1, ".xyz": 1}
    assert "Misc/notes.xyz" not in file_paths(inventory)


def test_images_are_excluded_by_default_and_optional(scan, college):
    default = scan(college)
    assert "CS229 ML/final_project/figures/loss_curve.png" not in file_paths(default)
    assert default.summary.excluded_image_files == 1
    assert ".png" not in default.report.unrecognized_extensions

    with_images = scan(college, config=InventoryConfig(include_images=True))
    by_path = {f.relative_path: f for f in with_images.files}
    assert by_path["CS229 ML/final_project/figures/loss_curve.png"].category is Category.IMAGE
    assert with_images.summary.images_included


def test_build_dirs_can_be_kept_and_names_excluded(scan, college):
    kept = scan(college, config=InventoryConfig(skip_build_dirs=False))
    assert "Java Course/out/run_output.txt" in file_paths(kept)

    excluded = scan(college, config=InventoryConfig(exclude_dir_names=frozenset({"stats"})))
    assert not any(p.startswith("Stats/") for p in file_paths(excluded))
    assert ("Stats", "user_excluded") in skipped_pairs(excluded)


def test_summary_statistics(scan, college):
    inventory = scan(college)
    summary = inventory.summary
    assert summary.total_files == len(EXPECTED_ACADEMIC_FILES)
    assert summary.total_size_bytes == sum(f.size_bytes for f in inventory.files)
    assert summary.directories_scanned == len(inventory.directories) == 19
    assert summary.files_by_category == {
        "archive": 1, "code": 9, "data": 2, "document": 4,
        "notebook": 5, "presentation": 1, "spreadsheet": 1,
    }
    assert summary.files_by_extension[".r"] == 2
    assert summary.files_by_language["Python"] == 4
    assert summary.skipped_entries == len(inventory.skipped)
    assert summary.skipped_by_reason["sensitive_name"] == 4
    assert summary.errors == 0
    root_id = inventory.report.roots[0].id
    assert summary.per_root[root_id]["files"] == summary.total_files


def test_output_is_deterministic(scan, college):
    def comparable(inventory: Inventory) -> dict:
        data = inventory.to_dict()
        data["report"].pop("started_at")
        data["report"].pop("finished_at")
        return data

    assert comparable(scan(college)) == comparable(scan(college))


def test_json_round_trip(scan, college):
    inventory = scan(college)
    assert Inventory.from_json(inventory.to_json()) == inventory
    json.loads(inventory.to_json())  # valid JSON


def test_jsonl_sink_round_trips_through_load_inventory(fake_system, college, tmp_path):
    plan = plan_targeted([college], system=fake_system)
    memory = MemorySink()
    run_scan(plan, sink=memory)
    out = tmp_path / "out"
    with JsonlSink(out) as sink:
        run_scan(plan, sink=sink)
    loaded = load_inventory(out)
    expected = memory.inventory()
    assert loaded.files == expected.files
    assert loaded.directories == expected.directories
    assert loaded.skipped == expected.skipped
    assert loaded.summary == expected.summary
    with pytest.raises(FileExistsError):
        JsonlSink(out)


def test_invalid_roots_raise_clear_errors(scan, tmp_path):
    with pytest.raises(FileNotFoundError):
        scan(tmp_path / "missing")
    a_file = tmp_path / "file.py"
    a_file.write_text("")
    with pytest.raises(NotADirectoryError):
        scan(a_file)
