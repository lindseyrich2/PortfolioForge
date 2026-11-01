"""Root discovery and full-computer scans, on a fake computer under tmp_path."""

from __future__ import annotations

import pytest

from conftest import rel_to
from portfolioforge.inventory import (
    FullScanNotApproved,
    FullScanOptions,
    MemorySink,
    ProtectedLocationError,
    RootOrigin,
    ScanMode,
    plan_full_computer,
    plan_targeted,
    run_scan,
)


def _roots(plan, base):
    return {(rel_to(base, r.path), r.origin) for r in plan.roots}


def _excluded(plan, base):
    return {(rel_to(base, e.path), e.reason.value) for e in plan.excluded}


def _scan_full(plan):
    sink = MemorySink()
    run_scan(plan, sink=sink, allow_full_computer=True)
    return sink.inventory()


def test_full_plan_finds_user_files_everywhere_without_a_folder_list(fake_computer):
    fc = fake_computer
    plan = plan_full_computer(system=fc.system)
    assert plan.mode is ScanMode.FULL_COMPUTER
    assert _roots(plan, fc.base) == {
        ("C/Users/student", RootOrigin.PROFILE),
        ("RedirectedDocs", RootOrigin.KNOWN_FOLDER),  # Documents moved outside the profile
        ("C/CS101", RootOrigin.DRIVE_TOP_LEVEL),  # unusual top-level location
    }


def test_full_plan_excludes_system_and_installed_locations(fake_computer):
    fc = fake_computer
    excluded = _excluded(plan_full_computer(system=fc.system), fc.base)
    assert {
        ("C/Windows", "protected_location"),
        ("C/Program Files", "protected_location"),
        ("C/ProgramData", "protected_location"),
        ("C/$Recycle.Bin", "protected_location"),
        ("C/Windows.old", "protected_location"),
        ("C/Python39", "installed_tool"),
        ("C/Users/student/Desktop", "overlapping_root"),
        ("C/Users/student/Downloads", "overlapping_root"),
    } <= excluded


def test_full_plan_lists_optional_locations_without_scanning_them(fake_computer):
    fc = fake_computer
    plan = plan_full_computer(system=fc.system)
    optional = {rel_to(fc.base, o.path): o for o in plan.optional}
    assert set(optional) == {
        "D", "E", "G", "N",
        "C/Users/OldAccount", "C/Users/Public",
        "C/Windows.old/Users/student",
    }
    assert optional["D"].includable and optional["D"].include_hint == "--include-drive D"
    assert optional["E"].includable  # removable drives can be opted in
    assert not optional["G"].includable  # virtual Google Drive drive
    assert not optional["N"].includable  # network drive
    assert optional["C/Windows.old/Users/student"].kind == "old_windows_profile"
    assert "C/Users/Default" not in optional


def test_full_scan_inventories_user_files_and_skips_protected_ones(fake_computer):
    fc = fake_computer
    inventory = _scan_full(plan_full_computer(system=fc.system))
    found = {rel_to(fc.base, f.absolute_path) for f in inventory.files}
    assert found == {
        "C/CS101/hw1.py",
        "C/Users/student/PycharmProjects/tictactoe/main.py",
        "C/Users/student/Documents/Essay.docx",
        "C/Users/student/Downloads/Untitled.ipynb",
        "C/Users/student/Desktop/Lab3.java",
        "C/Users/student/OneDrive/School/Stats/analysis.R",  # ordinary local folder
        "RedirectedDocs/notes.txt",
    }
    reasons = {(s.relative_path, s.reason.value) for s in inventory.skipped}
    assert {
        ("AppData", "credential_location"),
        (".ssh", "credential_location"),
        ("anaconda3", "installed_tool"),
    } <= reasons
    hints = {f.location_hint for f in inventory.files}
    assert {"User profile", "Documents"} <= hints


def test_additional_drives_require_opt_in(fake_computer):
    fc = fake_computer
    plan = plan_full_computer(FullScanOptions(include_drives=("d:",)), system=fc.system)
    assert ("D", RootOrigin.ADDITIONAL_DRIVE) in _roots(plan, fc.base)
    inventory = _scan_full(plan)
    found = {rel_to(fc.base, f.absolute_path) for f in inventory.files}
    assert "D/School/thesis.ipynb" in found
    assert "D/Program Files/Game/game.py" not in found
    assert ("Program Files", "protected_location") in {
        (s.relative_path, s.reason.value) for s in inventory.skipped
    }


@pytest.mark.parametrize("letter", ["G", "N"])
def test_cloud_and_network_drives_cannot_be_included(fake_computer, letter):
    with pytest.raises(ProtectedLocationError):
        plan_full_computer(FullScanOptions(include_drives=(letter,)), system=fake_computer.system)


def test_unknown_drive_is_rejected(fake_computer):
    with pytest.raises(ValueError):
        plan_full_computer(FullScanOptions(include_drives=("Q",)), system=fake_computer.system)


def test_old_profiles_require_explicit_approval(fake_computer):
    fc = fake_computer
    old = fc.c / "Windows.old" / "Users" / "student"
    other = fc.c / "Users" / "OldAccount"
    plan = plan_full_computer(
        FullScanOptions(include_locations=(str(old), str(other))), system=fc.system
    )
    roots = _roots(plan, fc.base)
    assert ("C/Windows.old/Users/student", RootOrigin.OTHER_PROFILE) in roots
    assert ("C/Users/OldAccount", RootOrigin.OTHER_PROFILE) in roots
    assert all(rel_to(fc.base, o.path) not in {"C/Users/OldAccount", "C/Windows.old/Users/student"}
               for o in plan.optional)
    found = {rel_to(fc.base, f.absolute_path) for f in _scan_full(plan).files}
    assert {"C/Windows.old/Users/student/Documents/old_hw.py", "C/Users/OldAccount/Documents/project.py"} <= found


def test_full_scan_requires_explicit_approval(fake_computer):
    plan = plan_full_computer(system=fake_computer.system)
    with pytest.raises(FullScanNotApproved):
        run_scan(plan, sink=MemorySink())


@pytest.mark.parametrize(
    "relative",
    ["C/Windows", "C/Windows/System32", "C/Program Files", "C/Users/student/AppData/Roaming",
     "C/Users/student/.ssh", "C/Windows.old"],
)
def test_protected_locations_cannot_be_targeted(fake_computer, relative):
    with pytest.raises(ProtectedLocationError):
        plan_targeted([fake_computer.base / relative], system=fake_computer.system)


def test_targeting_a_drive_root_still_applies_protections(fake_computer):
    fc = fake_computer
    sink = MemorySink()
    run_scan(plan_targeted([fc.c], system=fc.system), sink=sink)
    inventory = sink.inventory()
    found = {f.relative_path for f in inventory.files}
    assert "CS101/hw1.py" in found
    assert "Users/student/Documents/Essay.docx" in found
    assert not any(p.startswith(("Windows", "Program Files", "ProgramData", "Python39")) for p in found)
    reasons = {(s.relative_path, s.reason.value) for s in inventory.skipped}
    assert {
        ("Windows", "protected_location"),
        ("Users/OldAccount", "other_profile"),
        ("Users/Public", "other_profile"),
        ("Users/student/AppData", "credential_location"),
        ("Python39", "installed_tool"),
    } <= reasons


def test_excluded_paths_remove_matching_roots_from_the_plan(fake_computer):
    fc = fake_computer
    plan = plan_full_computer(system=fc.system, exclude_paths=[fc.c / "CS101"])
    assert ("C/CS101", RootOrigin.DRIVE_TOP_LEVEL) not in _roots(plan, fc.base)
    assert ("C/CS101", "user_excluded") in _excluded(plan, fc.base)
    found = {rel_to(fc.base, f.absolute_path) for f in _scan_full(plan).files}
    assert "C/CS101/hw1.py" not in found


def test_excluding_every_root_is_an_error(fake_system, college):
    with pytest.raises(ValueError):
        plan_targeted([college / "Stats"], system=fake_system, exclude_paths=[college])


def test_output_folder_is_never_scanned(fake_computer):
    fc = fake_computer
    output = fc.profile / "Documents" / "inventory-output"
    output.mkdir(parents=True)
    (output / "files.jsonl").write_text("")
    plan = plan_full_computer(system=fc.system, output_dir=output)
    inventory = _scan_full(plan)
    assert ("Documents/inventory-output", "output_dir") in {
        (s.relative_path, s.reason.value) for s in inventory.skipped
    }
