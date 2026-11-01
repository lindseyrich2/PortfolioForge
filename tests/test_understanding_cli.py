"""End-to-end command-line workflow with a synthetic project and a temporary data folder."""

from __future__ import annotations

import re

import pytest

from conftest import make_tree
from portfolioforge import paths
from portfolioforge.grouping import cli as gcli
from portfolioforge.grouping.store import load_run
from portfolioforge.inventory import JsonlSink, plan_targeted, run_scan
from portfolioforge.understanding import cli as ucli
from test_understanding import NOTEBOOK, _python_source


@pytest.fixture
def setup(tmp_path, monkeypatch, fake_system, capsys):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "pf-data"))
    root = make_tree(tmp_path / "Synthetic", {
        "Projects/SpamFilter/train.py": _python_source(tmp_path / "EXECUTED.txt"),
        "Projects/SpamFilter/explore.ipynb": NOTEBOOK,
        "Projects/SpamFilter/README.md": "# Spam Filter\n",
        "Projects/SpamFilter/requirements.txt": "numpy\n",
        "Personal/resume.docx": "docx",
        "Personal/tax return.pdf": "pdf",
    })
    with JsonlSink(paths.inventories_dir() / "20260301T000000Z") as sink:
        run_scan(plan_targeted([root], system=fake_system), sink=sink)
    assert gcli.main(["propose"]) == 0
    run = load_run("20260301T000000Z")
    groups = {g.working_title.text: g.id for g in run.groups}
    capsys.readouterr()
    return {"root": root, "groups": groups, "tmp": tmp_path}


def _cli(capsys, *args, answer=None):
    kwargs = {"input_fn": lambda prompt: answer} if answer is not None else {}
    code = ucli.main(list(args), **kwargs)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_full_preparation_workflow_with_dry_run(setup, capsys):
    spam = setup["groups"]["SpamFilter"]
    code, out, _ = _cli(capsys, "eligibility")
    assert code == 0 and "technical_candidate" in out and "personal_or_admin" in out

    code, _, err = _cli(capsys, "approve-files", spam, "--yes")
    assert code == 1 and "Accept the group first" in err
    assert gcli.main(["accept", spam]) == 0
    capsys.readouterr()

    code, out, _ = _cli(capsys, "select", spam)
    assert code == 0 and "train.py" in out and "Nothing has been read" in out

    code, out, _ = _cli(capsys, "approve-files", spam, answer="no")
    assert code == 1 and "Nothing was read" in out
    code, out, _ = _cli(capsys, "approve-files", spam, "--yes")
    assert code == 0 and "Approved 4 file(s)" in out

    code, out, _ = _cli(capsys, "extract", spam)
    assert code == 0 and "4 of 4 approved file(s) read" in out and "scikit-learn" in out
    assert not (setup["tmp"] / "EXECUTED.txt").exists()

    code, out, _ = _cli(capsys, "manifest", spam, "--provider", "anthropic")
    assert code == 0 and "EXTERNAL" in out and "claude-sonnet-5-5" in out  # reasonably priced default
    assert "not an exact charge" in out
    external_id = re.search(r"Disclosure manifest (m_\w+)", out).group(1)
    code, out, _ = _cli(capsys, "manifest", spam, "--provider", "anthropic", "--model", "claude-opus-5-5")
    assert code == 0 and "anthropic / claude-opus-5-5" in out  # Opus is an explicit option
    code, _, err = _cli(capsys, "dry-run", spam, external_id)
    assert code == 1 and "external provider" in err  # the dry-run command never sends for real
    code, out, _ = _cli(capsys, "manifest", spam, "--provider", "dry-run")
    assert code == 0 and "Provider / model:  dry-run / dry-run" in out and "Will NOT be sent" in out
    manifest_id = re.search(r"Disclosure manifest (m_\w+)", out).group(1)

    code, _, err = _cli(capsys, "dry-run", spam, manifest_id)
    assert code == 1 and "has not been approved" in err
    code, out, _ = _cli(capsys, "approve-manifest", spam, manifest_id, answer="no")
    assert code == 1
    code, out, _ = _cli(capsys, "approve-manifest", spam, manifest_id, "--yes")
    assert code == 0 and "Disclosure approved" in out
    code, out, _ = _cli(capsys, "dry-run", spam, manifest_id)
    assert code == 0 and "Sent to an AI provider: no" in out

    audit = (paths.data_dir() / "understanding" / "20260301T000000Z" / spam / "audit.jsonl").read_text()
    assert '"outcome": "refused"' in audit and '"outcome": "dry_run_not_sent"' in audit
    assert "naive Bayes" not in audit and "train.py" not in audit  # no content in the audit log

    code, out, _ = _cli(capsys, "status", spam)
    assert code == 0 and "approved" in out


def test_personal_documents_cannot_be_approved(setup, capsys):
    personal = setup["groups"]["Personal"]
    assert gcli.main(["accept", personal]) == 0
    capsys.readouterr()
    code, _, err = _cli(capsys, "approve-files", personal, "--yes")
    assert code == 1 and "personal_or_admin" in err
    assert not (paths.data_dir() / "understanding").exists()


def test_redacted_output_shows_no_names(setup, capsys):
    spam = setup["groups"]["SpamFilter"]
    gcli.main(["accept", spam])
    capsys.readouterr()
    outputs = []
    for args in (["eligibility"], ["select", spam], ["approve-files", spam, "--yes"], ["extract", spam],
                 ["manifest", spam, "--provider", "dry-run"], ["status", spam]):
        code, out, err = _cli(capsys, "--redact", *args)
        assert code == 0, err
        outputs.append(out + err)
    text = "\n".join(outputs)
    for name in ("SpamFilter", "train.py", "explore.ipynb", "Personal", "resume", str(setup["root"])):
        assert name not in text


def test_no_grouping_results_yet(tmp_path, monkeypatch, fake_system, capsys):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "pf-data"))
    root = make_tree(tmp_path / "S", {"a/x.py": ""})
    with JsonlSink(paths.inventories_dir() / "20260301T000000Z") as sink:
        run_scan(plan_targeted([root], system=fake_system), sink=sink)
    code, _, err = _cli(capsys, "eligibility")
    assert code == 1 and "No grouping results yet" in err
