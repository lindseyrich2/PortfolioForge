"""With --redact, no CLI output (including errors) shows paths or names.

All paths are synthetic, with distinctive marker names so that any leak is
easy to detect.
"""

from __future__ import annotations

import pytest

from conftest import make_tree
from portfolioforge import data, paths
from portfolioforge.inventory import cli

MARKER = "Zebra Secret Folder"


@pytest.fixture
def use_system(monkeypatch, fake_system):
    monkeypatch.setattr(cli, "detect_system", lambda: fake_system)
    return fake_system


def _run(capsys, args, **kwargs):
    code = cli.main(args, **kwargs)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _assert_hidden(text: str, *secrets) -> None:
    for secret in (MARKER, *secrets):
        assert str(secret) not in text


def test_missing_folder_error_is_redacted(use_system, tmp_path, capsys):
    missing = tmp_path / MARKER / "hw1"
    code, text = _run(capsys, [str(missing), "--output-dir", str(tmp_path / "out"), "--redact"])
    assert code == cli.EXIT_ERROR
    assert "a folder was not found (details hidden by --redact)" in text
    _assert_hidden(text, missing, tmp_path)


def test_without_redact_the_error_names_the_path(use_system, tmp_path, capsys):
    missing = tmp_path / MARKER
    code, text = _run(capsys, [str(missing), "--output-dir", str(tmp_path / "out")])
    assert code == cli.EXIT_ERROR
    assert MARKER in text  # useful detail on your own terminal


def test_not_a_folder_error_is_redacted(use_system, tmp_path, capsys):
    a_file = tmp_path / f"{MARKER}.py"
    a_file.write_text("")
    code, text = _run(capsys, [str(a_file), "--output-dir", str(tmp_path / "out"), "--redact"])
    assert code == cli.EXIT_ERROR
    assert "not a folder" in text
    _assert_hidden(text, a_file)


def test_protected_location_error_is_redacted(monkeypatch, fake_computer, tmp_path, capsys):
    monkeypatch.setattr(cli, "detect_system", lambda: fake_computer.system)
    target = fake_computer.c / "Windows"
    code, text = _run(capsys, [str(target), "--output-dir", str(tmp_path / "out"), "--redact"])
    assert code == cli.EXIT_ERROR
    assert "protected, private or linked location" in text
    _assert_hidden(text, target, fake_computer.base)


def test_private_settings_errors_are_redacted(use_system, college, tmp_path, capsys):
    missing_settings = tmp_path / MARKER / "privacy.toml"
    code, text = _run(capsys, [str(college), "--output-dir", str(tmp_path / "out"),
                               "--privacy-config", str(missing_settings), "--redact"])
    assert code == cli.EXIT_ERROR
    assert "privacy settings file is missing or invalid" in text
    _assert_hidden(text, missing_settings)


def test_existing_inventory_error_is_redacted(use_system, college, tmp_path, capsys):
    out = tmp_path / MARKER
    assert cli.main([str(college), "--output-dir", str(out), "--quiet"]) == cli.EXIT_OK
    capsys.readouterr()
    code, text = _run(capsys, [str(college), "--output-dir", str(out), "--quiet", "--redact"])
    assert code == cli.EXIT_ERROR
    assert "already contains an inventory" in text
    _assert_hidden(text, out, college)


def test_permission_error_is_redacted(use_system, college, tmp_path, capsys, monkeypatch):
    blocked = tmp_path / MARKER / "out"

    def deny(output_dir):
        raise PermissionError(13, "Access is denied", str(blocked))

    monkeypatch.setattr(cli, "JsonlSink", deny)
    code, text = _run(capsys, [str(college), "--output-dir", str(blocked), "--quiet", "--redact"])
    assert code == cli.EXIT_ERROR
    assert "permission denied (details hidden by --redact)" in text
    _assert_hidden(text, blocked, college)


def test_git_repository_output_error_is_redacted(use_system, college, tmp_path, capsys):
    repo = tmp_path / MARKER
    (repo / ".git").mkdir(parents=True)
    code, text = _run(capsys, [str(college), "--output-dir", str(repo / "out"), "--redact"])
    assert code == cli.EXIT_ERROR
    assert "inside a Git repository" in text
    _assert_hidden(text, repo)


def test_successful_redacted_run_shows_no_paths_or_file_names(use_system, college, tmp_path, capsys):
    out = tmp_path / MARKER
    code, text = _run(capsys, [str(college), "--output-dir", str(out), "--redact"])
    assert code == cli.EXIT_OK
    _assert_hidden(text, college, out, "CS101", "hw1.py", "credentials", "Untitled")


def test_default_output_location_is_shown_only_as_an_id(use_system, college, tmp_path, monkeypatch, capsys):
    data_home = tmp_path / MARKER
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(data_home))
    code, text = _run(capsys, [str(college), "--quiet", "--redact"])
    assert code == cli.EXIT_OK
    assert "<data dir>/inventories/" in text
    _assert_hidden(text, data_home, college)


@pytest.mark.parametrize("exc", [
    FileNotFoundError(2, "No such file", f"C:/{MARKER}/x"),
    PermissionError(13, "Denied", f"/{MARKER}/y"),
    OSError(5, "I/O error", f"/{MARKER}/z"),
    ValueError(f"Drive not found: {MARKER}"),
])
def test_every_redacted_error_message_omits_the_exception_text(exc):
    assert MARKER not in cli.format_error(exc, redact=True)


def test_data_command_errors_show_only_the_error_type(tmp_path, monkeypatch, capsys):
    data_home = make_tree(tmp_path / MARKER, {})
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(data_home))

    def broken(base=None):
        raise PermissionError(13, "Access is denied", str(data_home / "inventories"))

    monkeypatch.setattr(data, "stored_inventories", broken)
    assert data.main(["list"]) == data.EXIT_ERROR
    text = capsys.readouterr().err
    assert "PermissionError" in text
    _assert_hidden(text, data_home)
