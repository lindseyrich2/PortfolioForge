"""Repository privacy check. Every "personal" value here is synthetic, and
anything the check is meant to catch is assembled at run time, so this file
itself passes the check."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from portfolioforge.devtools import privacy_check as pc

SYNTHETIC_USER = "case" + "ywren"  # not a placeholder name, so it must be flagged
WIN_HOME = "C:" + "\\Users\\" + SYNTHETIC_USER
POSIX_HOME = "/home/" + SYNTHETIC_USER


def _rules(path: str, text: str | bytes, terms=()) -> set[str]:
    data = text if isinstance(text, bytes) else text.encode("utf-8")
    return {f.rule for f in pc.check_file(path, data, terms)}


@pytest.mark.parametrize("line", [
    f'root = r"{WIN_HOME}\\Documents"',
    f"see {WIN_HOME.replace(chr(92), '/')}/Desktop",
    f"export PATH={POSIX_HOME}/bin",
    "cd " + "/Users/" + SYNTHETIC_USER + "/Projects",
])
def test_real_looking_home_paths_are_flagged(line):
    assert "absolute_home_path" in _rules("src/example.py", line)


@pytest.mark.parametrize("line", [
    "C:" + "\\Users\\me\\PycharmProjects",
    "C:" + "\\Users\\<name>\\Documents",
    "/home/" + "student/projects",
    "C/Users/student/Documents/Essay.docx",  # relative test-fixture path, not absolute
    "%LOCALAPPDATA%\\PortfolioForge\\inventories",
])
def test_placeholder_paths_are_allowed(line):
    assert "absolute_home_path" not in _rules("README.md", line)


def test_local_identity_terms_are_flagged_without_being_printed():
    findings = pc.check_file("tests/x.py", f"owner = '{SYNTHETIC_USER}'".encode(), {SYNTHETIC_USER})
    assert [f.rule for f in findings] == ["local_identity"]
    assert SYNTHETIC_USER not in str(findings[0])


def test_identity_terms_match_whole_words_only():
    assert "local_identity" not in _rules("x.py", "unrelated words", {"word"})
    assert "local_identity" in _rules("x.py", "a word here", {"word"})


def test_local_identity_terms_come_from_this_computer_and_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("USERNAME", "synthuser")
    monkeypatch.setenv("USER", "synthuser")
    monkeypatch.setenv(pc.PRIVATE_TERMS_ENV, "Course Alpha 101, ab")
    monkeypatch.setenv("PORTFOLIOFORGE_CONFIG_DIR", str(tmp_path))
    (tmp_path / pc.PRIVATE_TERMS_FILE).write_text("# comment\nSecret Lab Folder\n", encoding="utf-8")
    terms = pc.local_identity_terms()
    assert {"synthuser", "Course Alpha 101", "Secret Lab Folder"} <= terms
    assert "ab" not in terms  # too short to match safely


@pytest.mark.parametrize(("path", "rule"), [
    ("inventories/2026/files.jsonl", "personal_data_file"),
    ("notes/report.json", "personal_data_file"),
    ("config/privacy.toml", "personal_data_file"),
    ("data/train.csv", "data_file"),
    ("analysis/model.rds", "data_file"),
    ("notebooks/Untitled.ipynb", "data_file"),
])
def test_personal_data_and_dataset_files_are_flagged(path, rule):
    assert rule in _rules(path, "x")


def test_inventory_shaped_json_is_flagged():
    record = json.dumps({"root_id": "r_1", "relative_path": "a/b.py", "name": "b.py"})
    assert "inventory_data" in _rules("tests/fixture.json", record)
    assert "inventory_data" not in _rules("tests/settings.json", json.dumps({"theme": "dark"}))


def test_large_files_are_flagged():
    assert "large_file" in _rules("docs/big.txt", b"a" * (pc.MAX_FILE_BYTES + 1))


@pytest.mark.parametrize(("text", "rule"), [
    ("-----BEGIN " + "RSA " + "PRIVATE " + "KEY-----", "private_key"),
    ("token = gh" + "p_" + "A1b2C3d4" * 5, "github_token"),
    ("key = AK" + "IA" + "ABCDEFGHIJKLMNOP", "aws_access_key"),
    ("api_key = '" + "x" * 20 + "'", "generic_secret"),
])
def test_secrets_are_flagged(text, rule):
    assert rule in _rules("src/settings.py", text)


def test_binary_files_only_get_name_checks():
    assert _rules("assets/logo.png", b"\x89PNG\0\0" + WIN_HOME.encode()) == set()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_check_runs_against_a_git_repository(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "clean.py").write_text("print('hello')\n")
    (repo / ".gitignore").write_text("ignored/\n")
    (repo / "ignored").mkdir()
    (repo / "ignored" / "files.jsonl").write_text("{}\n")  # ignored: not checked
    assert pc.main(["--root", str(repo), "--ci"]) == 0

    (repo / "leak.py").write_text(f"HOME = r'{WIN_HOME}'\n")
    assert pc.main(["--root", str(repo), "--ci"]) == 1
    printed = capsys.readouterr().out
    assert "leak.py:1: absolute_home_path" in printed
    assert SYNTHETIC_USER not in printed


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_this_repository_passes_the_check():
    repo_root = Path(__file__).resolve().parents[1]
    if not (repo_root / ".git").exists():
        pytest.skip("not running from a Git checkout")
    assert pc.run_check(repo_root, include_identity=False) == []
