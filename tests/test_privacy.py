"""Private folders, storage locations, and output safety. Synthetic data only."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from conftest import file_paths, make_tree, skipped_pairs
from portfolioforge import paths
from portfolioforge.inventory import (
    InventoryConfig,
    JsonlSink,
    ProtectedLocationError,
    cli,
    plan_full_computer,
    plan_targeted,
    scan_inventory,
)
from portfolioforge.privacy import (
    DEFAULT_PRIVATE_FOLDER_NAMES,
    PrivacyConfigError,
    PrivacySettings,
    load_privacy_settings,
)

# --- private folders are skipped entirely ---------------------------------------

PERSONAL_TREE = {
    "Documents/Coursework/hw1.py": "",
    "Documents/Taxes/2019 return.pdf": "",
    "Documents/Medical Records/visit summary.pdf": "",
    "Documents/Job Applications/cover letter.docx": "",
    "Documents/Coursework/private/notes.txt": "",
    "Scans/Personal/id-card.pdf": "",
}


@pytest.fixture
def personal(tmp_path: Path) -> Path:
    return make_tree(tmp_path / "home", PERSONAL_TREE)


def test_default_private_folders_are_skipped_without_being_listed(fake_system, personal, monkeypatch):
    listed: list[str] = []
    real_scandir = os.scandir

    def recording_scandir(path="."):
        listed.append(os.path.basename(os.fspath(path)))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", recording_scandir)
    inventory = scan_inventory(personal, system=fake_system)
    monkeypatch.undo()

    found = file_paths(inventory)
    assert "Documents/Coursework/hw1.py" in found
    assert not any(p.startswith(("Documents/Taxes", "Documents/Medical Records")) for p in found)
    assert "Documents/Coursework/private/notes.txt" not in found
    assert {"Taxes", "Medical Records", "private"}.isdisjoint(listed)
    private = [s for s in inventory.skipped if s.reason.value == "private_folder"]
    assert sorted(s.relative_path for s in private) == [
        "Documents/Coursework/[private]",
        "Documents/[private]",
        "Documents/[private]",
    ]
    exported = inventory.to_json()
    for secret in ("Taxes", "Medical Records", "2019 return", "visit summary"):
        assert secret not in exported


def test_user_private_names_and_paths_are_skipped(fake_system, personal):
    settings = PrivacySettings(
        private_paths=(str(personal / "Scans" / "Personal"),),
        private_folder_names=DEFAULT_PRIVATE_FOLDER_NAMES | {"job applications"},
    )
    inventory = scan_inventory(personal, system=fake_system, config=InventoryConfig().with_privacy(settings))
    assert file_paths(inventory) == {"Documents/Coursework/hw1.py"}
    exported = inventory.to_json()
    assert "Job Applications" not in exported and "id-card" not in exported
    assert ("Scans/[private]", "private_folder") in skipped_pairs(inventory)


def test_private_root_is_refused_without_repeating_its_path(fake_system, personal):
    with pytest.raises(ProtectedLocationError) as excinfo:
        plan_targeted([personal / "Documents" / "Taxes"], system=fake_system)
    assert "Taxes" not in str(excinfo.value)

    secret = personal / "Scans" / "Personal"
    with pytest.raises(ProtectedLocationError) as excinfo:
        plan_targeted([secret], system=fake_system, private_paths=[str(personal / "Scans")])
    assert str(secret) not in str(excinfo.value)


def test_full_plan_drops_private_roots_and_optional_locations(fake_computer):
    fc = fake_computer
    redirected = fc.base / "RedirectedDocs"
    old_account = fc.c / "Users" / "OldAccount"
    plan = plan_full_computer(system=fc.system, private_paths=[str(redirected), str(old_account)])
    assert str(redirected) not in {r.path for r in plan.roots}
    assert ("[private]", "private_folder") in {(e.path, e.reason.value) for e in plan.excluded}
    shown = repr(plan)
    assert str(redirected) not in shown
    assert str(old_account) not in {o.path for o in plan.optional}


# --- privacy settings file ------------------------------------------------------


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "privacy.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_privacy_settings_file_is_layered_on_defaults(tmp_path):
    private_dir = (tmp_path / "Private Scans").as_posix()
    settings = load_privacy_settings(_write(tmp_path, f"""
[private_folders]
paths = ["{private_dir}"]
names = ["Job Applications"]
allow_default_names = ["Insurance"]
"""))
    assert settings.private_paths == (os.path.abspath(private_dir),)
    assert "job applications" in settings.private_folder_names
    assert "insurance" not in settings.private_folder_names
    assert "taxes" in settings.private_folder_names


def test_default_names_can_be_turned_off(tmp_path):
    settings = load_privacy_settings(_write(tmp_path, "[private_folders]\nuse_default_names = false\n"))
    assert settings.private_folder_names == frozenset()


@pytest.mark.parametrize(
    "text",
    [
        '[private_folders]\npaths = ["relative/folder"]\n',
        "[private_folders]\nnames = \"not a list\"\n",
        "[private_folders]\nunknown_key = 1\n",
        "[private_folders]\nuse_default_names = \"yes\"\n",
        "this is not toml = = =\n",
    ],
)
def test_invalid_privacy_settings_are_rejected(tmp_path, text):
    with pytest.raises(PrivacyConfigError):
        load_privacy_settings(_write(tmp_path, text))


def test_missing_settings_file(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "empty-config"))
    assert load_privacy_settings() == PrivacySettings()  # default file absent: defaults
    with pytest.raises(PrivacyConfigError):
        load_privacy_settings(tmp_path / "does-not-exist.toml")  # explicit file must exist


# --- storage locations ----------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "env", "expected_data", "expected_config"),
    [
        ("win32", {"LOCALAPPDATA": "L", "APPDATA": "R"}, Path("L/PortfolioForge"), Path("R/PortfolioForge")),
        ("linux", {"XDG_DATA_HOME": "D", "XDG_CONFIG_HOME": "C"}, Path("D/portfolioforge"), Path("C/portfolioforge")),
    ],
)
def test_platform_storage_locations(monkeypatch, platform, env, expected_data, expected_config):
    for name in ("LOCALAPPDATA", "APPDATA", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
                 paths.DATA_DIR_ENV, paths.CONFIG_DIR_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "platform", platform)
    assert paths.data_dir() == expected_data
    assert paths.config_dir() == expected_config


def test_platform_defaults_without_environment(monkeypatch, tmp_path):
    for name in ("LOCALAPPDATA", "APPDATA", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
                 paths.DATA_DIR_ENV, paths.CONFIG_DIR_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(paths, "_home", lambda: tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert paths.data_dir() == tmp_path / "Library" / "Application Support" / "PortfolioForge"
    monkeypatch.setattr(sys, "platform", "linux")
    assert paths.data_dir() == tmp_path / ".local" / "share" / "portfolioforge"
    assert paths.config_dir() == tmp_path / ".config" / "portfolioforge"


def test_environment_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "d"))
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "c"))
    assert paths.data_dir() == tmp_path / "d"
    assert paths.inventories_dir() == tmp_path / "d" / "inventories"
    assert paths.config_dir() == tmp_path / "c"


# --- never inside a Git repository ------------------------------------------------


@pytest.fixture
def git_tree(tmp_path: Path) -> Path:
    repo = tmp_path / "some-repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def test_output_inside_a_git_repository_is_refused(git_tree):
    assert paths.find_git_work_tree(git_tree / "nested" / "not-yet-created") == git_tree
    with pytest.raises(paths.UnsafeOutputLocation):
        JsonlSink(git_tree / "inventories" / "run")
    assert not (git_tree / "inventories").exists()


def test_output_outside_a_repository_is_allowed(tmp_path):
    outside = tmp_path / "plain" / "out"
    assert paths.find_git_work_tree(outside) is None
    with JsonlSink(outside):
        pass
    assert outside.is_dir()


def test_cli_refuses_output_in_a_repository(monkeypatch, fake_system, college, git_tree, capsys):
    monkeypatch.setattr(cli, "detect_system", lambda: fake_system)
    code = cli.main([str(college), "--output-dir", str(git_tree / "out"), "--quiet"])
    assert code == cli.EXIT_ERROR
    assert "Git repository" in capsys.readouterr().err
    assert not (git_tree / "out").exists()


# --- command-line privacy options ----------------------------------------------


def test_cli_redact_shows_no_paths(monkeypatch, fake_system, college, tmp_path, capsys):
    monkeypatch.setattr(cli, "detect_system", lambda: fake_system)
    out = tmp_path / "out"
    code = cli.main([str(college), "--output-dir", str(out), "--quiet", "--redact"])
    assert code == cli.EXIT_OK
    printed = capsys.readouterr().out
    assert str(college) not in printed and str(out) not in printed
    assert "[user_selected] root 1" in printed


def test_cli_uses_privacy_settings_file(monkeypatch, fake_system, college, tmp_path):
    from portfolioforge.inventory import load_inventory

    monkeypatch.setattr(cli, "detect_system", lambda: fake_system)
    settings = _write(tmp_path, '[private_folders]\nnames = ["Stats"]\n')
    out = tmp_path / "out"
    code = cli.main([str(college), "--output-dir", str(out), "--quiet", "--privacy-config", str(settings)])
    assert code == cli.EXIT_OK
    assert not any(p.startswith("Stats/") for p in file_paths(load_inventory(out)))


def test_cli_rejects_a_broken_privacy_file(monkeypatch, fake_system, college, tmp_path, capsys):
    monkeypatch.setattr(cli, "detect_system", lambda: fake_system)
    broken = _write(tmp_path, "[private_folders]\nnames = 5\n")
    code = cli.main([str(college), "--output-dir", str(tmp_path / "out"), "--privacy-config", str(broken)])
    assert code == cli.EXIT_ERROR
    assert not (tmp_path / "out").exists()
