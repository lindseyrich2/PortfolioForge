"""Safety guarantees: read-only, metadata-only, no links, no cloud, bounded work."""

from __future__ import annotations

import builtins
import dataclasses
import io
import os
import pathlib
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import file_paths, make_tree, skipped_pairs
from portfolioforge.inventory import MemorySink, ResourceLimits, plan_targeted, run_scan
from portfolioforge.inventory.platform import (
    FILE_ATTRIBUTE_HIDDEN,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_RECALL_ON_OPEN,
    FILE_ATTRIBUTE_SYSTEM,
)


def _snapshot(base: Path) -> dict[str, tuple[bytes, int]]:
    return {
        p.relative_to(base).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in base.rglob("*")
        if p.is_file()
    }


def test_scan_does_not_modify_anything(scan, college):
    before = _snapshot(college)
    dirs_before = sorted(p.relative_to(college).as_posix() for p in college.rglob("*") if p.is_dir())
    scan(college)
    assert _snapshot(college) == before
    assert sorted(p.relative_to(college).as_posix() for p in college.rglob("*") if p.is_dir()) == dirs_before


def test_scan_never_opens_executes_writes_or_connects(scan, college, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the scanner must not open, run, change or send anything")

    for target, name in [
        (builtins, "open"), (io, "open"), (os, "open"), (os, "system"), (os, "remove"),
        (os, "unlink"), (os, "rename"), (os, "replace"), (os, "rmdir"), (os, "mkdir"),
        (os, "makedirs"), (os, "utime"), (os, "chmod"), (os, "execv"), (os, "execve"),
        (subprocess, "Popen"), (socket, "socket"), (socket, "create_connection"),
        (pathlib.Path, "open"), (pathlib.Path, "read_bytes"), (pathlib.Path, "read_text"),
        (pathlib.Path, "write_bytes"), (pathlib.Path, "write_text"), (pathlib.Path, "touch"),
    ]:
        monkeypatch.setattr(target, name, forbidden)
    if hasattr(os, "startfile"):
        monkeypatch.setattr(os, "startfile", forbidden)

    inventory = scan(college)
    monkeypatch.undo()
    assert inventory.summary.total_files > 0


def test_symlinks_are_recorded_but_never_followed(scan, tmp_path):
    outside = make_tree(tmp_path / "outside", {"secret_project.py": "", "notes.txt": ""})
    root = make_tree(tmp_path / "root", {"real.py": ""})
    try:
        os.symlink(outside, root / "shortcut", target_is_directory=True)
        os.symlink(outside / "notes.txt", root / "linked.txt")
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symlinks")
    inventory = scan(root)
    assert file_paths(inventory) == {"real.py"}
    assert {("shortcut", "link"), ("linked.txt", "link")} <= skipped_pairs(inventory)


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are Windows-only")
def test_junctions_are_recorded_but_never_followed(scan, tmp_path):
    import _winapi

    outside = make_tree(tmp_path / "outside", {"secret_project.py": ""})
    root = make_tree(tmp_path / "root", {"real.py": ""})
    _winapi.CreateJunction(str(outside), str(root / "junction"))
    inventory = scan(root)
    assert file_paths(inventory) == {"real.py"}
    assert ("junction", "link") in skipped_pairs(inventory)


def test_targeting_a_link_is_refused(fake_system, tmp_path):
    from portfolioforge.inventory import ProtectedLocationError

    target = make_tree(tmp_path / "target", {"a.py": ""})
    try:
        os.symlink(target, tmp_path / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symlinks")
    with pytest.raises(ProtectedLocationError):
        plan_targeted([tmp_path / "link"], system=fake_system)


def test_cloud_only_placeholders_are_never_listed_or_inventoried(scan, tmp_path, monkeypatch):
    root = make_tree(
        tmp_path / "OneDrive",
        {
            "Local.docx": "",
            "Cloud.docx": "",
            "Pinned/notes.pdf": "",
            "CloudFolder/inner.py": "",
        },
    )

    def attributes(path: str, st: os.stat_result) -> int:
        name = os.path.basename(path)
        if name == "Cloud.docx":
            return FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        if name == "CloudFolder":
            return FILE_ATTRIBUTE_RECALL_ON_OPEN
        return 0

    listed: list[str] = []
    real_scandir = os.scandir

    def recording_scandir(path="."):
        listed.append(os.path.basename(os.fspath(path)))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", recording_scandir)
    inventory = scan(root, attributes=attributes)

    assert file_paths(inventory) == {"Local.docx", "Pinned/notes.pdf"}
    assert inventory.summary.cloud_only_files_skipped == 1
    assert ("CloudFolder", "cloud_only") in skipped_pairs(inventory)
    assert "CloudFolder" not in listed
    assert "Cloud.docx" not in inventory.to_json()


def test_windows_hidden_and_system_attributes_are_respected(scan, tmp_path):
    root = make_tree(tmp_path / "root", {"visible.py": "", "hidden.py": "", "System Stuff/x.py": ""})

    def attributes(path: str, st: os.stat_result) -> int:
        name = os.path.basename(path)
        return {"hidden.py": FILE_ATTRIBUTE_HIDDEN, "System Stuff": FILE_ATTRIBUTE_SYSTEM}.get(name, 0)

    inventory = scan(root, attributes=attributes)
    assert file_paths(inventory) == {"visible.py"}
    assert {("hidden.py", "hidden"), ("System Stuff", "system")} <= skipped_pairs(inventory)


def test_permission_errors_are_recorded_and_the_scan_continues(scan, college, monkeypatch):
    real_scandir = os.scandir

    def failing_scandir(path="."):
        if os.path.basename(os.fspath(path)) == "CS229 ML":
            raise PermissionError(13, "Access is denied", os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", failing_scandir)
    inventory = scan(college)
    assert ("CS229 ML", "permission_error") in skipped_pairs(inventory)
    assert not any(p.startswith("CS229 ML/") for p in file_paths(inventory))
    assert "CS101 Intro/hw1.py" in file_paths(inventory)
    assert inventory.summary.errors == 1
    assert inventory.summary.errors_by_type == {"PermissionError": 1}
    error = next(s for s in inventory.skipped if s.reason.value == "permission_error")
    assert error.detail == "PermissionError"  # exception type only, never the message


def test_the_same_folder_is_never_scanned_twice(fake_system, college):
    plan = plan_targeted([college], system=fake_system)
    nested = dataclasses.replace(plan.roots[0], id="r_nested", path=str(college / "CS101 Intro"))
    overlapping = dataclasses.replace(plan, roots=(plan.roots[0], nested))
    sink = MemorySink()
    run_scan(overlapping, sink=sink)
    inventory = sink.inventory()
    assert ("", "already_scanned") in {
        (s.relative_path, s.reason.value) for s in inventory.skipped if s.root_id == "r_nested"
    }
    assert not any(f.root_id == "r_nested" for f in inventory.files)


def test_overlapping_and_duplicate_roots_are_merged(fake_system, college):
    plan = plan_targeted([college, college, college / "CS101 Intro"], system=fake_system)
    assert [r.path for r in plan.roots] == [str(college)]
    assert [e.reason.value for e in plan.excluded] == ["overlapping_root", "overlapping_root"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows paths are case-insensitive")
def test_roots_differing_only_in_case_are_merged(fake_system, college):
    plan = plan_targeted([college, str(college).upper()], system=fake_system)
    assert len(plan.roots) == 1


# --- resource limits -----------------------------------------------------------


def _run(plan, **kwargs):
    sink = MemorySink()
    report = run_scan(plan, sink=sink, **kwargs)
    return report, sink.inventory()


def test_max_files_stops_the_scan_and_reports_it(fake_system, college):
    plan = plan_targeted([college], system=fake_system)
    report, inventory = _run(plan, limits=ResourceLimits(max_files=3))
    assert len(inventory.files) == 3
    assert (report.complete, report.stop_reason) == (False, "max_files")


def test_max_directories_stops_the_scan(fake_system, college):
    plan = plan_targeted([college], system=fake_system)
    report, inventory = _run(plan, limits=ResourceLimits(max_directories=2))
    assert inventory.summary.directories_scanned == 2
    assert (report.complete, report.stop_reason) == (False, "max_directories")


def test_max_depth_skips_deep_folders_and_marks_the_scan_incomplete(fake_system, college):
    plan = plan_targeted([college], system=fake_system)
    report, inventory = _run(plan, limits=ResourceLimits(max_depth=1))
    assert "CS101 Intro/hw1.py" in file_paths(inventory)
    assert "CS101 Intro/hw3/main.py" not in file_paths(inventory)
    assert ("CS101 Intro/hw3", "max_depth") in skipped_pairs(inventory)
    assert report.stop_reason is None
    assert report.limits_reached == ("max_depth",)
    assert not report.complete


def test_time_budget_stops_the_scan(fake_system, college):
    ticks = iter(range(0, 10_000, 10))
    plan = plan_targeted([college], system=fake_system)
    report, _ = _run(plan, limits=ResourceLimits(time_budget_seconds=25), clock=lambda: next(ticks))
    assert (report.complete, report.stop_reason) == (False, "time_budget")


def test_should_stop_cancels_the_scan(fake_system, college):
    plan = plan_targeted([college], system=fake_system)
    report, _ = _run(plan, should_stop=lambda: True)
    assert (report.complete, report.stop_reason) == (False, "cancelled")


def test_throttling_pauses_between_entries(fake_system, college):
    pauses: list[float] = []
    plan = plan_targeted([college], system=fake_system)
    _run(plan, limits=ResourceLimits(throttle_every=5, throttle_seconds=0.01), sleep=pauses.append)
    assert pauses and set(pauses) == {0.01}


def test_background_priority_is_entered_and_restored():
    from portfolioforge.inventory.platform import background_priority

    with background_priority():
        pass
    with background_priority(enabled=False):
        pass


def test_progress_reports_counts_but_never_file_names(fake_system, college):
    events = []
    plan = plan_targeted([college], system=fake_system)
    _run(plan, on_progress=events.append, limits=ResourceLimits(progress_interval_seconds=0))
    assert len(events) > 2
    assert events[-1].finished
    assert events[-1].files_inventoried == 23
    text = repr(events)
    for name in ("credentials", "id_rsa", "server.pem", "hw1.py", "Untitled"):
        assert name not in text
