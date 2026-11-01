"""Candidate grouping heuristics, on synthetic academic inventories only."""

from __future__ import annotations

import builtins
import io
import os
import random
import socket
import subprocess
from dataclasses import replace

import pytest

from grouping_fixtures import make_inventory
from portfolioforge.grouping import (
    FlagKind,
    GroupingConfig,
    GroupKind,
    RelationKind,
    config_from_dict,
    propose_groups,
)
from portfolioforge.grouping.config import GroupingConfigError


def _paths(inventory):
    return {f.id: f.relative_path for f in inventory.files}


def _by_title(run):
    return {(g.user_title or g.working_title.text): g for g in run.groups}


def _member_paths(group, inventory):
    paths = _paths(inventory)
    return sorted(paths[m.file_id] for m in group.members)


def _flags(run, group, kind):
    return [f for f in run.flags if f.target == group.id and f.kind is kind]


COURSE = {
    "Fall 2021/CS 101 Intro/hw1.py": 0,
    "Fall 2021/CS 101 Intro/hw1_writeup.pdf": 0.3,
    "Fall 2021/CS 101 Intro/hw2.py": 7,
    "Fall 2021/CS 101 Intro/hw3/main.py": 14,
    "Fall 2021/CS 101 Intro/hw3/input.txt": 14,
    "Fall 2021/CS 101 Intro/Lab 3/lab3.py": 20,
    "Fall 2021/CS 101 Intro/Lab 3/lab3 report.docx": 21,
}


# --- hierarchy and assignments ----------------------------------------------------


def test_multiple_assignments_in_one_course_folder():
    inv = make_inventory(COURSE)
    run = propose_groups(inv)
    groups = _by_title(run)
    term, course = groups["Fall 2021"], groups["CS 101 Intro"]
    assert term.kind is course.kind is GroupKind.COURSE_COLLECTION
    assert course.parent_id == term.id
    assert course.course_hints == ("CS 101",)
    assert _member_paths(groups["hw 1"], inv) == ["Fall 2021/CS 101 Intro/hw1.py",
                                                  "Fall 2021/CS 101 Intro/hw1_writeup.pdf"]
    assert _member_paths(groups["hw 2"], inv) == ["Fall 2021/CS 101 Intro/hw2.py"]
    assert len(groups["hw3"].members) == 2 and len(groups["Lab 3"].members) == 2
    for title in ("hw 1", "hw 2", "hw3", "Lab 3"):
        assert groups[title].kind is GroupKind.ASSIGNMENT
        assert groups[title].parent_id == course.id
    assert not run.unassigned


def test_different_assignment_numbers_are_never_joined():
    inv = make_inventory({"Course CS 200/hw4.py": 0, "Course CS 200/hw5.py": 0.01,
                          "Course CS 200/hw4 graphs.pdf": 0.01, "Course CS 200/hw5 graphs.pdf": 0.01})
    groups = _by_title(propose_groups(inv))
    assert _member_paths(groups["hw 4"], inv) == ["Course CS 200/hw4 graphs.pdf", "Course CS 200/hw4.py"]
    assert _member_paths(groups["hw 5"], inv) == ["Course CS 200/hw5 graphs.pdf", "Course CS 200/hw5.py"]


def test_structured_project_is_one_group_with_marker_evidence():
    inv = make_inventory(
        {"Projects/TextClassifier/src/model.py": 5, "Projects/TextClassifier/src/utils/io.py": 5,
         "Projects/TextClassifier/data/train.csv": 5, "Projects/TextClassifier/README.md": 5},
        marker_files={"home/Projects/TextClassifier": ["README.md", "requirements.txt"]},
        marker_dirs={"home/Projects/TextClassifier": [".git"]},
        skipped_dirs={"home/Projects/TextClassifier/venv": "virtual_env"},
    )
    run = propose_groups(inv)
    (group,) = [g for g in run.groups if g.kind is not GroupKind.COURSE_COLLECTION]
    assert group.working_title.text == "TextClassifier" and group.kind is GroupKind.PROJECT
    assert len(group.members) == 4 and group.band.value == "high"
    kinds = {e.kind for e in group.evidence}
    assert {"version_control", "dependency_file", "readme", "virtual_env", "shared_folder"} <= kinds


def test_schema_v1_inventories_use_inventoried_and_skipped_markers():
    inv = make_inventory(
        {"Projects/Sorter/sort.py": 1, "Projects/Sorter/README.md": 1, "Projects/Sorter/requirements.txt": 1},
        skipped_dirs={"home/Projects/Sorter/.git": "hidden"}, schema_version=1,
    )
    (group,) = [g for g in propose_groups(inv).groups]
    assert {"version_control", "dependency_file", "readme"} <= {e.kind for e in group.evidence}


def test_nested_project_inside_an_assignment_becomes_a_child_group():
    inv = make_inventory(
        {"BIO 210/Lab 5/lab5_writeup.pdf": 10, "BIO 210/Lab 5/PopulationSimulator/simulate.R": 9,
         "BIO 210/Lab 5/PopulationSimulator/README.md": 9, "BIO 210/Lab 2/lab2.R": 3},
        marker_files={"home/BIO 210/Lab 5/PopulationSimulator": ["README.md", "DESCRIPTION"]},
        marker_dirs={"home/BIO 210/Lab 5/PopulationSimulator": [".git"]},
    )
    groups = _by_title(propose_groups(inv))
    assert groups["PopulationSimulator"].parent_id == groups["Lab 5"].id
    assert _member_paths(groups["Lab 5"], inv) == ["BIO 210/Lab 5/lab5_writeup.pdf"]


def test_container_folders_never_become_projects():
    inv = make_inventory({"Downloads/hw7.py": 1, "Downloads/hw7 notes.pdf": 1.1, "Downloads/syllabus.pdf": 60})
    run = propose_groups(inv)
    assert "Downloads" not in _by_title(run)
    assert _member_paths(_by_title(run)["hw 7"], inv) == ["Downloads/hw7 notes.pdf", "Downloads/hw7.py"]
    assert [_paths(inv)[u.file_id] for u in run.unassigned] == ["Downloads/syllabus.pdf"]


# --- generic names, timestamps ------------------------------------------------------


def test_generic_notebooks_in_different_folders_are_never_merged():
    inv = make_inventory({"Downloads/Untitled.ipynb": 1, "Documents/Untitled.ipynb": 1,
                          "Desktop/Untitled1.ipynb": 1})
    run = propose_groups(inv)
    assert not [g for g in run.groups if len(g.members) > 1]
    assert not run.relationships
    assert len(run.unassigned) == 3


def test_generic_names_in_a_container_need_more_than_time_to_join():
    inv = make_inventory({"Downloads/Untitled.ipynb": 1, "Downloads/Untitled1.ipynb": 1.001,
                          "Downloads/main.py": 1.002})
    run = propose_groups(inv)
    assert not [g for g in run.groups if g.kind is not GroupKind.LOOSE_COLLECTION]


def test_a_plain_folder_of_generic_files_is_grouped_by_folder_with_low_confidence():
    inv = make_inventory({"Scratch Work/Untitled.ipynb": 1, "Scratch Work/Untitled1.ipynb": 30})
    run = propose_groups(inv)
    group = _by_title(run)["Scratch Work"]
    assert len(group.members) == 2 and group.band.value == "low"
    assert _flags(run, group, FlagKind.GENERIC_NAMES)


def test_bulk_copied_timestamps_are_not_used_as_evidence():
    files = {f"Archive/topic{i} alpha{i}.py": 5 for i in range(12)}
    run = propose_groups(make_inventory(files))
    assert not [g for g in run.groups if g.kind is not GroupKind.LOOSE_COLLECTION]
    assert not any(e.kind == "very_close_in_time" for g in run.groups for m in g.members for e in m.evidence)


# --- cross-folder relationships ---------------------------------------------------------


def test_report_in_documents_is_linked_to_code_elsewhere_but_not_merged():
    inv = make_inventory({
        "Documents/Sentiment Analysis/Final Report.pdf": 40,
        "Documents/Sentiment Analysis/slides.pptx": 41,
        "PycharmProjects/SentimentAnalysis/train_sentiment.py": 30,
        "PycharmProjects/SentimentAnalysis/sentiment_model.py": 35,
    })
    run = propose_groups(inv)
    groups = _by_title(run)
    report, code = groups["Sentiment Analysis"], groups["SentimentAnalysis"]
    (rel,) = [r for r in run.relationships if {r.source, r.target} == {report.id, code.id}]
    assert rel.kind is RelationKind.REPORT_FOR and rel.status == "suggested"
    assert {e.kind for e in rel.evidence} >= {"rare_shared_token", "overlapping_time"}
    assert report.id != code.id and len(report.members) == 2 and len(code.members) == 2  # not merged


def test_common_words_alone_do_not_link_folders():
    inv = make_inventory({"Documents/Final Project/report.pdf": 1, "PycharmProjects/Final Project v2/main.py": 2,
                          "Documents/Other/notes.txt": 1, "PycharmProjects/Thing/thing.py": 1})
    assert not propose_groups(inv).relationships


def test_shared_course_code_across_folders_is_a_same_course_suggestion():
    inv = make_inventory({"Documents/CS 230 Notes/week notes.pdf": 10,
                          "PycharmProjects/cs230-search/search.py": 12})
    (rel,) = propose_groups(inv).relationships
    assert rel.kind is RelationKind.SAME_COURSE and not rel.suggest_merge


def test_unassigned_files_get_nearest_group_suggestions():
    inv = make_inventory({"PycharmProjects/GraphColoring/coloring.py": 1,
                          "PycharmProjects/GraphColoring/graph_io.py": 1,
                          "Documents/graph coloring writeup.pdf": 2})
    run = propose_groups(inv)
    (loose,) = run.unassigned
    assert loose.nearest and loose.nearest[0][0] == _by_title(run)["GraphColoring"].id


# --- duplicates, third-party, templates ---------------------------------------------


def _project(prefix: str, size: int = 500, day: float = 3) -> dict:
    return {f"{prefix}/{name}": {"day": day, "size": size + i} for i, name in
            enumerate(["model.py", "train.py", "README.md", "data/train.csv"])}


def test_duplicate_copies_are_flagged_and_all_kept():
    inv = make_inventory({**_project("Projects/Traffic Model"), **_project("Projects/Traffic Model - Copy", day=9),
                          **_project("Projects/Traffic Model (2)", day=12)})
    run = propose_groups(inv)
    groups = _by_title(run)
    original = groups["Traffic Model"]
    dups = [r for r in run.relationships if r.kind is RelationKind.POSSIBLE_DUPLICATE]
    assert len(dups) == 3
    assert all(r.likely_original == original.id for r in dups if original.id in (r.source, r.target))
    for title in ("Traffic Model", "Traffic Model - Copy", "Traffic Model (2)"):
        assert len(groups[title].members) == 4
        assert _flags(run, groups[title], FlagKind.POSSIBLE_DUPLICATE)


def test_same_layout_with_different_sizes_is_a_variant():
    inv = make_inventory({**_project("Projects/Maze Solver"), **_project("Projects/Maze Solver old", size=900)})
    kinds = {r.kind for r in propose_groups(inv).relationships}
    assert RelationKind.POSSIBLE_VARIANT in kinds and RelationKind.POSSIBLE_DUPLICATE not in kinds


def test_third_party_signals_are_flagged_without_asserting_ownership():
    files = {f"Projects/vendored-lib/module{i}.py": 100 for i in range(12)}
    inv = make_inventory(files, marker_files={"home/Projects/vendored-lib": ["LICENSE", "CONTRIBUTING.md"]},
                         marker_dirs={"home/Projects/vendored-lib": [".git", ".github"]})
    run = propose_groups(inv)
    group = _by_title(run)["vendored-lib"]
    (flag,) = _flags(run, group, FlagKind.POSSIBLE_THIRD_PARTY)
    assert flag.severity == "review"
    assert {e.kind for e in flag.evidence} >= {"third_party_vcs", "third_party_community_files"}
    assert group.labels == ()  # never labelled automatically


def test_own_repository_with_only_git_is_not_flagged_third_party():
    inv = make_inventory({"Projects/my-sorter/sort.py": 1, "Projects/my-sorter/test_sort.py": 4},
                         marker_dirs={"home/Projects/my-sorter": [".git"]})
    run = propose_groups(inv)
    assert not [f for f in run.flags if f.kind is FlagKind.POSSIBLE_THIRD_PARTY]


def test_known_third_party_names_come_from_user_settings():
    inv = make_inventory({"Projects/somelib/a.py": 1, "Projects/somelib/b.py": 2})
    assert not [f for f in propose_groups(inv).flags if f.kind is FlagKind.POSSIBLE_THIRD_PARTY]
    config = config_from_dict({"vocabulary": {"known_third_party_names": ["somelib"]}})
    assert [f for f in propose_groups(inv, config).flags if f.kind is FlagKind.POSSIBLE_THIRD_PARTY]


def test_template_folders_are_flagged():
    inv = make_inventory({"Desktop/GameStarterTemplate/src/Main.java": 2, "Desktop/GameStarterTemplate/src/Player.java": 2})
    run = propose_groups(inv)
    assert _flags(run, _by_title(run)["GameStarterTemplate"], FlagKind.POSSIBLE_TEMPLATE)


def test_large_datasets_are_flagged():
    inv = make_inventory({"Projects/Weather/model.py": 1,
                          "Projects/Weather/data/raw.csv": {"day": 1, "size": 200 * 1024 * 1024}})
    run = propose_groups(inv)
    assert _flags(run, _by_title(run)["Weather"], FlagKind.LARGE_DATASET)


def test_plain_folder_with_several_assignments_is_split():
    inv = make_inventory({"Intro Programming/hw1.py": 1, "Intro Programming/hw2.py": 8,
                          "Intro Programming/hw2 notes.txt": 8.1})
    groups = _by_title(propose_groups(inv))
    assert "Intro Programming" not in groups
    assert len(groups["hw 1"].members) == 1 and len(groups["hw 2"].members) == 2


def test_strong_project_with_several_assignments_is_kept_and_flagged_mixed():
    inv = make_inventory({"Projects/scratch/hw1.py": 1, "Projects/scratch/hw2.py": 8, "Projects/scratch/util.py": 3},
                         marker_files={"home/Projects/scratch": ["requirements.txt"]},
                         marker_dirs={"home/Projects/scratch": [".git"]})
    run = propose_groups(inv)
    group = _by_title(run)["scratch"]
    assert len(group.members) == 3
    assert _flags(run, group, FlagKind.MIXED_CONTENT)


# --- configuration ------------------------------------------------------------------


def test_custom_vocabulary_from_settings():
    inv = make_inventory({"Kurs/aufgabe1.py": 1, "Kurs/aufgabe1 notizen.pdf": 1.1, "Kurs/aufgabe2.py": 9})
    default_titles = set(_by_title(propose_groups(inv)))
    assert "aufgabe 1" not in default_titles
    config = config_from_dict({"vocabulary": {"assignment_words": ["aufgabe"], "collection_words": ["kurs"]}})
    groups = _by_title(propose_groups(inv, config))
    assert len(groups["aufgabe 1"].members) == 2 and len(groups["aufgabe 2"].members) == 1


def test_course_code_pattern_can_be_overridden():
    # single-letter course codes ("Q7") are not matched by the default pattern
    inv = make_inventory({"Documents/Q7 notes/x.pdf": 1, "PycharmProjects/q7-solver/y.py": 1})
    assert not propose_groups(inv).relationships
    config = config_from_dict({"patterns": {"course_code": r"(?i)\b(Q)[ -]?(\d)\b"}})
    assert [r.kind for r in propose_groups(inv, config).relationships] == [RelationKind.SAME_COURSE]


def test_one_shared_word_is_not_enough_for_a_link():
    inv = make_inventory({"Documents/Math notes/a.pdf": 1, "PycharmProjects/math tools/b.py": 1})
    assert not propose_groups(inv).relationships


@pytest.mark.parametrize("data", [
    {"unknown": {}},
    {"vocabulary": {"nonsense": ["x"]}},
    {"vocabulary": {"assignment_words": "hw"}},
    {"patterns": {"course_code": "("}},
    {"weights": {"not_a_weight": 1}},
    {"thresholds": {"cluster_link": "high"}},
])
def test_invalid_grouping_settings_are_rejected(data):
    with pytest.raises(GroupingConfigError):
        config_from_dict(data)


# --- guarantees -----------------------------------------------------------------------


def test_every_file_is_kept_exactly_once_on_random_trees():
    words = ["hw1", "hw2", "Lab 3", "report", "Untitled", "main", "analysis", "graph", "sorting", "data"]
    folders = ["", "Fall 2022", "Fall 2022/CS 310", "Projects", "Projects/alpha", "Downloads", "misc/deep/er"]
    exts = [".py", ".ipynb", ".pdf", ".csv", ".R", ".docx"]
    for seed in range(25):
        rng = random.Random(seed)
        files = {}
        for i in range(rng.randint(1, 40)):
            folder = rng.choice(folders)
            name = f"{rng.choice(words)}{rng.choice(['', '_v2', ' - Copy', str(i)])}{rng.choice(exts)}"
            files[f"{folder}/{name}".lstrip("/")] = {"day": rng.uniform(0, 200), "size": rng.randint(1, 5000)}
        inv = make_inventory(files)
        run = propose_groups(inv)  # raises GroupingInvariantError if a file is lost or duplicated
        placed = [m.file_id for g in run.groups for m in g.members] + [u.file_id for u in run.unassigned]
        assert sorted(placed) == sorted(f.id for f in inv.files)


def test_every_group_and_membership_is_explained_and_scored():
    run = propose_groups(make_inventory({**COURSE, **_project("Projects/Traffic Model")}))
    for g in run.groups:
        assert g.evidence and 0.0 <= g.confidence <= 1.0
        assert g.working_title.provisional
        for m in g.members:
            assert m.evidence and 0.0 <= m.score <= 1.0
    for r in run.relationships:
        assert r.evidence


def test_output_is_deterministic_and_independent_of_input_order():
    inv = make_inventory({**COURSE, **_project("Projects/Traffic Model"), **_project("Projects/Traffic Model - Copy")})
    shuffled = replace(inv, files=tuple(random.Random(7).sample(inv.files, len(inv.files))),
                       directories=tuple(reversed(inv.directories)))

    def comparable(run):
        data = run.to_dict()
        data.pop("created_at")
        return data

    assert comparable(propose_groups(inv)) == comparable(propose_groups(inv)) == comparable(propose_groups(shuffled))


def test_grouping_run_round_trips_through_json():
    from portfolioforge.grouping import GroupingRun

    run = propose_groups(make_inventory({**COURSE, **_project("Projects/Traffic Model")}))
    assert GroupingRun.from_dict(run.to_dict()).to_dict() == run.to_dict()


def test_grouping_never_touches_files_network_or_processes(monkeypatch):
    inv = make_inventory({**COURSE, **_project("Projects/Traffic Model")})

    def forbidden(*args, **kwargs):
        raise AssertionError("grouping must not open files, list folders, run programs or connect")

    for target, name in [(builtins, "open"), (io, "open"), (os, "open"), (os, "scandir"), (os, "listdir"),
                         (os, "stat"), (subprocess, "Popen"), (socket, "socket"), (socket, "create_connection")]:
        monkeypatch.setattr(target, name, forbidden)
    run = propose_groups(inv)
    monkeypatch.undo()
    assert run.groups


def test_default_settings_contain_no_personal_data():
    config = GroupingConfig()
    assert config.known_third_party_names == frozenset()
    assert config.digest() == GroupingConfig().digest()
