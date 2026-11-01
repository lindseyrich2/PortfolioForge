"""Command line behavior. detect_system is always replaced by a fake system."""

from __future__ import annotations

import pytest

from portfolioforge.inventory import cli, load_inventory


@pytest.fixture
def use_system(monkeypatch):
    def _use(system):
        monkeypatch.setattr(cli, "detect_system", lambda: system)

    return _use


def _never_asked(prompt: str) -> str:
    raise AssertionError("should not prompt")


def test_targeted_scan_writes_a_local_inventory(use_system, fake_system, college, tmp_path, capsys):
    use_system(fake_system)
    out = tmp_path / "out"
    code = cli.main([str(college), "--output-dir", str(out), "--quiet"], input_fn=_never_asked)
    assert code == cli.EXIT_OK
    inventory = load_inventory(out)
    assert inventory.summary.total_files == 23
    assert (out / "plan.json").exists()
    output = capsys.readouterr().out
    assert "Scan plan (targeted)" in output and "Scan finished." in output


def test_incomplete_scans_are_reported_clearly(use_system, fake_system, college, tmp_path, capsys):
    use_system(fake_system)
    code = cli.main(
        [str(college), "--output-dir", str(tmp_path / "out"), "--quiet", "--max-files", "2"],
        input_fn=_never_asked,
    )
    assert code == cli.EXIT_INCOMPLETE
    output = capsys.readouterr().out
    assert "Scan INCOMPLETE." in output and "max_files" in output


def test_no_arguments_is_an_error():
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2


def test_protected_target_is_refused(use_system, fake_computer, tmp_path, capsys):
    use_system(fake_computer.system)
    code = cli.main([str(fake_computer.c / "Windows"), "--output-dir", str(tmp_path / "out")])
    assert code == cli.EXIT_ERROR
    assert "protected" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_full_computer_plan_only_scans_nothing(use_system, fake_computer, tmp_path, capsys):
    use_system(fake_computer.system)
    out = tmp_path / "out"
    code = cli.main(["--full-computer", "--plan-only", "--output-dir", str(out)], input_fn=_never_asked)
    assert code == cli.EXIT_OK
    assert not out.exists()
    output = capsys.readouterr().out
    assert "Scan plan (full computer)" in output
    assert "--include-drive D" in output
    assert "Plan only: nothing was scanned." in output


def test_exclude_option_removes_roots_from_the_plan(use_system, fake_computer, tmp_path, capsys):
    use_system(fake_computer.system)
    excluded = fake_computer.c / "CS101"
    code = cli.main(
        ["--full-computer", "--plan-only", "--exclude", str(excluded), "--output-dir", str(tmp_path / "out")],
        input_fn=_never_asked,
    )
    assert code == cli.EXIT_OK
    output = capsys.readouterr().out
    roots_section = output.split("Excluded locations")[0]
    assert str(excluded) not in roots_section
    assert f"[user_excluded] {excluded}" in output


def test_full_computer_scan_needs_a_yes(use_system, fake_computer, tmp_path, capsys):
    use_system(fake_computer.system)
    out = tmp_path / "out"
    code = cli.main(["--full-computer", "--output-dir", str(out)], input_fn=lambda prompt: "no")
    assert code == cli.EXIT_ERROR
    assert not out.exists()
    assert "Scan cancelled" in capsys.readouterr().out


def test_full_computer_scan_runs_after_confirmation(use_system, fake_computer, tmp_path):
    use_system(fake_computer.system)
    out = tmp_path / "out"
    prompts: list[str] = []

    def confirm(prompt: str) -> str:
        prompts.append(prompt)
        return "yes"

    code = cli.main(
        ["--full-computer", "--output-dir", str(out), "--quiet", "--normal-priority"],
        input_fn=confirm,
    )
    assert code == cli.EXIT_OK
    assert len(prompts) == 1
    names = {f.name for f in load_inventory(out).files}
    assert {"hw1.py", "main.py", "Essay.docx", "notes.txt"} <= names


def test_include_options_require_full_computer(use_system, fake_system, college):
    use_system(fake_system)
    with pytest.raises(SystemExit):
        cli.main([str(college), "--include-drive", "D"])
