"""Data management commands, against a synthetic data folder."""

from __future__ import annotations

from pathlib import Path

import pytest

from portfolioforge import data, paths
from portfolioforge.inventory import JsonlSink, plan_targeted, run_scan


@pytest.fixture
def data_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "pf-data"
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(home))
    return home


@pytest.fixture
def two_inventories(data_home, fake_system, college) -> list[str]:
    plan = plan_targeted([college], system=fake_system)
    ids = ["20250101T000000Z", "20260101T000000Z"]
    for inventory_id in ids:
        with JsonlSink(paths.inventories_dir() / inventory_id) as sink:
            run_scan(plan, sink=sink)
    return ids


def _never_asked(prompt: str) -> str:
    raise AssertionError("should not prompt")


def _assert_no_personal_details(text: str, college: Path) -> None:
    assert str(college) not in text
    for name in ("CS101", "Untitled", "hw1.py", "College", "Lab Reports"):
        assert name not in text


def test_list_shows_ids_and_counts_only(two_inventories, college, capsys):
    assert data.main(["list"]) == data.EXIT_OK
    printed = capsys.readouterr().out
    assert all(i in printed for i in two_inventories)
    assert "23" in printed
    _assert_no_personal_details(printed, college)


def test_stats_are_counts_only(two_inventories, college, capsys):
    assert data.main(["stats", "latest"]) == data.EXIT_OK
    printed = capsys.readouterr().out
    assert "Inventory 20260101T000000Z" in printed
    assert "Files inventoried:   23" in printed
    assert "Scan roots:          1 (user_selected 1)" in printed
    _assert_no_personal_details(printed, college)


def test_stats_for_unknown_inventory(two_inventories, capsys):
    assert data.main(["stats", "nope"]) == data.EXIT_ERROR


def test_purge_deletes_only_the_chosen_inventory_and_no_original_files(two_inventories, college):
    before = sorted(p.as_posix() for p in college.rglob("*"))
    assert data.main(["purge", two_inventories[0], "--yes"], input_fn=_never_asked) == data.EXIT_OK
    assert not (paths.inventories_dir() / two_inventories[0]).exists()
    assert (paths.inventories_dir() / two_inventories[1]).exists()
    assert sorted(p.as_posix() for p in college.rglob("*")) == before


def test_purge_asks_first(two_inventories):
    assert data.main(["purge", "--all"], input_fn=lambda prompt: "no") == data.EXIT_ERROR
    assert all((paths.inventories_dir() / i).exists() for i in two_inventories)
    assert data.main(["purge", "--all"], input_fn=lambda prompt: "delete") == data.EXIT_OK
    assert not any(paths.inventories_dir().iterdir())


def test_purge_refuses_folders_with_unexpected_files(two_inventories, capsys):
    target = paths.inventories_dir() / two_inventories[0]
    (target / "my-notes.txt").write_text("not PortfolioForge output")
    assert data.main(["purge", two_inventories[0], "--yes"]) == data.EXIT_ERROR
    assert (target / "my-notes.txt").exists() and (target / "report.json").exists()


def test_purge_rejects_unknown_or_path_like_ids(two_inventories):
    assert data.main(["purge", "../pf-data", "--yes"]) == data.EXIT_ERROR
    assert data.main(["purge", "missing", "--yes"]) == data.EXIT_ERROR
    assert all((paths.inventories_dir() / i).exists() for i in two_inventories)


def test_purge_older_than(two_inventories):
    assert data.main(["purge", "--older-than", "3650", "--yes"]) == data.EXIT_OK
    assert all((paths.inventories_dir() / i).exists() for i in two_inventories)  # all are recent
    assert data.main(["purge", "--older-than", "0", "--yes"]) == data.EXIT_OK
    assert not any(paths.inventories_dir().iterdir())


def test_unfinished_inventories_are_listed_and_purgeable(data_home, capsys):
    unfinished = paths.inventories_dir() / "20260202T000000Z"
    unfinished.mkdir(parents=True)
    (unfinished / "files.jsonl").write_text("")
    assert data.main(["list"]) == data.EXIT_OK
    assert "(unfinished)" in capsys.readouterr().out
    assert data.main(["purge", "20260202T000000Z", "--yes"]) == data.EXIT_OK
    assert not unfinished.exists()


def test_empty_data_folder(data_home, capsys):
    assert data.main(["list"]) == data.EXIT_OK
    assert "No saved inventories." in capsys.readouterr().out
    assert data.main(["stats"]) == data.EXIT_ERROR


def test_where_reports_locations(data_home, capsys):
    assert data.main(["where"]) == data.EXIT_OK
    printed = capsys.readouterr().out
    assert str(data_home) in printed and "Keep them private" in printed
