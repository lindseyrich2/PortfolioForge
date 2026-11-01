"""Review decisions, local storage and the grouping command line. Synthetic data only."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from grouping_fixtures import make_inventory
from portfolioforge import paths
from portfolioforge.grouping import (
    Decision,
    DecisionError,
    GroupKind,
    GroupStatus,
    RelationKind,
    append_decision,
    load_decisions,
    propose_groups,
)
from portfolioforge.grouping import cli as gcli
from portfolioforge.grouping.store import groupings_dir, load_run, save_run
from portfolioforge.inventory import JsonlSink

FILES = {
    "Fall 2021/CS 101 Intro/hw1.py": 0,
    "Fall 2021/CS 101 Intro/hw1_writeup.pdf": 0.3,
    "Fall 2021/CS 101 Intro/hw2.py": 7,
    "Fall 2021/CS 101 Intro/hw3/main.py": 14,
    "Projects/Traffic Model/model.py": {"day": 3, "size": 500},
    "Projects/Traffic Model/README.md": {"day": 3, "size": 501},
    "Projects/Traffic Model - Copy/model.py": {"day": 9, "size": 500},
    "Projects/Traffic Model - Copy/README.md": {"day": 9, "size": 501},
    "Downloads/traffic model notes.pdf": 4,
}


@pytest.fixture
def inv():
    return make_inventory(FILES)


def _ids(inventory):
    return {f.relative_path: f.id for f in inventory.files}


def _group(run, title):
    return next(g for g in run.groups if (g.user_title or g.working_title.text) == title)


def _decision(action, *targets, value=None, n=0):
    return Decision(f"d_{action}_{n}", action, targets, value, datetime(2026, 1, 1, tzinfo=UTC))


def _rerun(inventory, *decisions):
    return propose_groups(inventory, decisions=decisions)


# --- decisions ------------------------------------------------------------------


def test_merge_split_move_and_unassign(inv):
    base = propose_groups(inv)
    ids = _ids(inv)
    hw1, hw2 = _group(base, "hw 1"), _group(base, "hw 2")

    merged = _rerun(inv, _decision("merge", hw1.id, hw2.id))
    assert merged.group(hw2.id) is None
    assert len(merged.group(hw1.id).members) == 3

    split = _rerun(inv, _decision("split", hw1.id, ids["Fall 2021/CS 101 Intro/hw1_writeup.pdf"], value="hw1 report"))
    report = _group(split, "hw1 report")
    assert [m.file_id for m in report.members] == [ids["Fall 2021/CS 101 Intro/hw1_writeup.pdf"]]
    assert report.parent_id == hw1.parent_id

    notes = ids["Downloads/traffic model notes.pdf"]
    house = _group(base, "Traffic Model")
    moved = _rerun(inv, _decision("move", notes, house.id))
    assert notes in moved.group(house.id).file_ids
    assert notes not in {u.file_id for u in moved.unassigned}

    unassigned = _rerun(inv, _decision("unassign", ids["Fall 2021/CS 101 Intro/hw2.py"]))
    assert unassigned.group(hw2.id) is None  # empty groups disappear
    assert any(u.reason == "unassigned by you" for u in unassigned.unassigned)


def test_accept_reject_rename_and_labels(inv):
    base = propose_groups(inv)
    hw1, house = _group(base, "hw 1"), _group(base, "Traffic Model")
    run = _rerun(inv, _decision("accept", hw1.id), _decision("rename", hw1.id, value="Intro recursion exercise"),
                 _decision("label", house.id, value="mine"), _decision("reject", _group(base, "hw 2").id))
    renamed = run.group(hw1.id)
    assert renamed.status is GroupStatus.ACCEPTED and renamed.user_title == "Intro recursion exercise"
    assert renamed.working_title.provisional  # a user title is still not a final repository name
    assert run.group(house.id).labels == ("mine",)
    assert any(u.reason == "its proposed group was rejected by you" for u in run.unassigned)
    relabelled = _rerun(inv, _decision("label", house.id, value="mine"),
                        _decision("label", house.id, value="third_party", n=1))
    assert relabelled.group(house.id).labels == ("third_party",)


def test_relationship_decisions(inv):
    base = propose_groups(inv)
    original, copy = _group(base, "Traffic Model"), _group(base, "Traffic Model - Copy")
    (dup,) = [r for r in base.relationships if r.kind is RelationKind.POSSIBLE_DUPLICATE
              and {r.source, r.target} == {original.id, copy.id}]
    assert dup.likely_original == original.id
    kept = _rerun(inv, _decision("keep_both", original.id, copy.id))
    assert next(r for r in kept.relationships if r.id == dup.id).status == "dismissed"
    assert not [f for f in kept.flags if f.kind.value == "possible_duplicate"]
    confirmed = _rerun(inv, _decision("confirm", dup.id))
    assert next(r for r in confirmed.relationships if r.id == dup.id).status == "confirmed"
    hw1, hw2 = _group(base, "hw 1"), _group(base, "hw 2")
    marked = _rerun(inv, _decision("mark_duplicate", hw1.id, hw2.id))
    assert [r.status for r in marked.relationships if {r.source, r.target} == {hw1.id, hw2.id}] == ["confirmed"]


def test_decisions_survive_reruns_and_become_stale_when_targets_disappear(inv):
    base = propose_groups(inv)
    hw2 = _group(base, "hw 2")
    decisions = [_decision("rename", hw2.id, value="Second exercise")]
    assert _group(_rerun(inv, *decisions), "Second exercise").id == hw2.id  # stable ids
    smaller = replace(inv, files=tuple(f for f in inv.files if f.name != "hw2.py"))
    run = propose_groups(smaller, decisions=decisions)
    assert run.stale_decisions == ("d_rename_0",)


def test_cross_folder_relationships_are_never_merged_automatically(inv):
    run = propose_groups(inv)
    for rel in run.relationships:
        assert rel.status == "suggested"
    assert len({g.id for g in run.groups}) == len(run.groups)


@pytest.mark.parametrize(("action", "targets", "value"), [
    ("explode", ("g_x",), None),
    ("merge", ("g_x",), None),
    ("rename", ("g_x",), "  "),
    ("label", ("g_x",), "owner"),
    ("move", ("f_x",), None),
])
def test_invalid_decisions_are_rejected(tmp_path, action, targets, value):
    with pytest.raises(DecisionError):
        append_decision(tmp_path / "decisions.jsonl", action, targets, value)


def test_decision_ledger_round_trips(tmp_path):
    path = tmp_path / "reviews" / "decisions.jsonl"
    first = append_decision(path, "accept", ["g_1"])
    second = append_decision(path, "rename", ["g_1"], "Sorting visualizer")
    assert [d.id for d in load_decisions(path)] == [first.id, second.id]
    assert load_decisions(path)[1].value == "Sorting visualizer"


# --- storage ----------------------------------------------------------------------


@pytest.fixture
def data_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "pf-data"
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(home))
    return home


def test_runs_are_stored_in_the_data_folder(data_home, inv):
    run = propose_groups(inv, inventory_key="20260101T000000Z")
    path = save_run(run, "20260101T000000Z")
    assert path.parent == data_home / "groupings" / "20260101T000000Z"
    assert load_run("20260101T000000Z").to_dict() == run.to_dict()


def test_results_are_never_written_inside_a_git_repository(tmp_path, monkeypatch, inv):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(repo / "data"))
    with pytest.raises(paths.UnsafeOutputLocation):
        save_run(propose_groups(inv), "x")
    with pytest.raises(paths.UnsafeOutputLocation):
        append_decision(repo / "data" / "reviews" / "x" / "decisions.jsonl", "accept", ["g_1"])
    assert not (repo / "data").exists()


@pytest.mark.parametrize("key", ["", "..", "a/b", "a\\b"])
def test_invalid_inventory_keys_are_rejected(key):
    with pytest.raises(ValueError):
        groupings_dir(key)


# --- command line -------------------------------------------------------------------


def _save_inventory(inventory, inventory_id: str) -> None:
    with JsonlSink(paths.inventories_dir() / inventory_id) as sink:
        for record in inventory.files:
            sink.add_file(record)
        for record in inventory.directories:
            sink.add_directory(record)
        for entry in inventory.skipped:
            sink.add_skipped(entry)
        sink.close(inventory.report)


def _cli(capsys, *args):
    code = gcli.main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_propose_list_show_and_decide(data_home, inv, capsys):
    _save_inventory(inv, "20260101T000000Z")
    code, out, _ = _cli(capsys, "propose")
    assert code == 0 and "hw 1" in out and "course_collection" in out
    run = load_run("20260101T000000Z")
    hw1, hw2 = _group(run, "hw 1"), _group(run, "hw 2")

    code, out, _ = _cli(capsys, "show", hw1.id)
    assert code == 0 and "Why these files belong together" in out and "hw1_writeup.pdf" in out
    assert "provisional" in out

    code, out, _ = _cli(capsys, "merge", hw1.id, hw2.id)
    assert code == 0 and "applied" in out
    assert load_run("20260101T000000Z").group(hw2.id) is None
    code, out, _ = _cli(capsys, "decisions")
    assert "merge" in out

    code, _, err = _cli(capsys, "accept", "g_doesnotexist")
    assert code == 1 and "Unknown group" in err


def test_cli_redacted_listing_shows_no_names(data_home, inv, capsys):
    _save_inventory(inv, "20260101T000000Z")
    code, out, _ = _cli(capsys, "--redact", "propose")
    assert code == 0
    for name in ("CS 101", "hw1", "Traffic Model", "Downloads", "Fall 2021"):
        assert name not in out
    run = load_run("20260101T000000Z")
    code, out, _ = _cli(capsys, "--redact", "show", _group(run, "Traffic Model").id)
    assert code == 0 and "Traffic Model" not in out and "model.py" not in out


def test_cli_report_is_written_locally(data_home, inv, capsys):
    _save_inventory(inv, "20260101T000000Z")
    code, out, _ = _cli(capsys, "report")
    report = data_home / "groupings" / "20260101T000000Z" / "report.md"
    assert code == 0 and report.is_file()
    text = report.read_text(encoding="utf-8")
    assert "Keep it on this computer" in text and "## Groups" in text


def test_cli_without_an_inventory(data_home, capsys):
    code, _, err = _cli(capsys, "propose")
    assert code == 1 and "No saved inventory" in err


def test_cli_uses_grouping_settings_file(data_home, tmp_path, capsys):
    inventory = make_inventory({"Kurs/aufgabe1.py": 1, "Kurs/aufgabe2.py": 9})
    _save_inventory(inventory, "20260101T000000Z")
    settings = tmp_path / "grouping.toml"
    settings.write_text('[vocabulary]\nassignment_words = ["aufgabe"]\n', encoding="utf-8")
    code, out, _ = _cli(capsys, "--config", str(settings), "propose")
    assert code == 0 and "aufgabe 1" in out and "aufgabe 2" in out


def test_cli_groups_a_real_v2_scan_of_a_synthetic_tree(data_home, fake_system, college, capsys):
    from portfolioforge.inventory import plan_targeted, run_scan

    with JsonlSink(paths.inventories_dir() / "20260202T000000Z") as sink:
        run_scan(plan_targeted([college], system=fake_system), sink=sink)
    code, out, _ = _cli(capsys, "--inventory", "20260202T000000Z", "propose")
    assert code == 0
    run = load_run("20260202T000000Z")
    placed = sum(len(g.members) for g in run.groups) + len(run.unassigned)
    assert placed == run.stats["files"] == 23
    assert any(g.kind is GroupKind.COURSE_COLLECTION for g in run.groups)
