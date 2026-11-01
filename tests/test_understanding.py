"""Project-understanding preparation: eligibility, approval, extraction, disclosure.

All projects are synthetic and written to temporary folders at test time.
Anything that resembles a personal path or a secret is assembled at run time.
"""

from __future__ import annotations

import builtins
import json
import os
import socket
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import make_tree
from grouping_fixtures import make_inventory
from portfolioforge import paths
from portfolioforge.grouping import Decision, propose_groups
from portfolioforge.grouping.models import GroupStatus
from portfolioforge.inventory import scan_inventory
from portfolioforge.privacy import PrivacySettings
from portfolioforge.understanding import (
    ApprovalError,
    ApprovedFileReader,
    DisclosureNotApproved,
    DryRunProvider,
    EligibilityStatus,
    ProviderNotConfigured,
    ReadRefused,
    approve_files,
    approve_manifest,
    assess_run,
    build_evidence_pack,
    build_manifest,
    get_provider,
    propose_selection,
    redact,
    submit,
)

FAKE_HOME = "C:" + "\\Users\\" + "casey" + "wren"  # assembled so this file passes the privacy check
FAKE_SECRET = "sk_" + "live_" + "A1b2C3d4E5f6G7h8"
KEY_NAME = "api" + "_key"  # assembled so the privacy check does not flag this file


def _python_source(sentinel: Path) -> str:
    return f'''r"""Spam filter training script.

Author: Example Student <example.student@example.org>
Data: {FAKE_HOME}\\Documents\\messages.csv
"""
# Email: example.student@example.org
import pathlib
import numpy as np
from sklearn.naive_bayes import MultinomialNB

{KEY_NAME.upper()} = "{FAKE_SECRET}"
pathlib.Path(r"{sentinel}").write_text("this code was executed")
raise SystemExit("discovered code must never run")


def train(messages, {KEY_NAME}="{FAKE_SECRET}"):
    """Train a naive Bayes spam classifier."""
    return MultinomialNB().fit(messages, None)


class SpamModel:
    pass
'''


NOTEBOOK = json.dumps({
    "cells": [
        {"cell_type": "markdown", "source": ["# Exploring message lengths\n", "Plots of lengths."]},
        {"cell_type": "code", "source": ["import pandas as pd\n", "df = pd.read_csv('x.csv')\n"],
         "outputs": [{"output_type": "stream", "text": ["SECRET OUTPUT VALUE 12345\n"]}]},
        {"cell_type": "code", "source": ["%matplotlib inline\n", "def plot_lengths(df):\n", "    return df\n"],
         "outputs": []},
    ],
    "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
})


@pytest.fixture
def world(tmp_path, fake_system):
    """A synthetic computer folder with one technical project and personal documents."""
    sentinel = tmp_path / "EXECUTED.txt"
    root = make_tree(tmp_path / "Synthetic", {
        "Projects/SpamFilter/train.py": _python_source(sentinel),
        "Projects/SpamFilter/explore.ipynb": NOTEBOOK,
        "Projects/SpamFilter/queries.sql": "CREATE TABLE messages (id int);\nSELECT * FROM messages m JOIN labels l ON m.id = l.id;\n",
        "Projects/SpamFilter/README.md": "# Spam Filter\n\nClassifies messages as spam or not spam.\n",
        "Projects/SpamFilter/requirements.txt": "numpy>=1.24\nscikit-learn==1.3\n# comment\n",
        "Projects/SpamFilter/data/messages.csv": "text,label\n",
        "Personal/resume.docx": "docx",
        "Personal/tax return.pdf": "pdf",
        "JavaWork/src/Main.java": "package demo;\nimport javax.swing.JFrame;\n/** Demo app. */\npublic class Main {\n"
                                  "    public static void main(String[] args) {\n    }\n}\n",
        "JavaWork/README.md": "# Java demo\n",
        "RWork/model.R": "#' Fit a model\nlibrary(ggplot2)\nfit_model <- function(df) {\n  stats::lm(y ~ x, df)\n}\n",
        "RWork/report.Rmd": "# Results\n\n```{r setup}\nlibrary(dplyr)\n```\n",
    })
    inventory = scan_inventory(root, system=fake_system)
    run = propose_groups(inventory)
    groups = {g.working_title.text: g for g in run.groups}
    return {"root": root, "inventory": inventory, "run": run, "groups": groups, "sentinel": sentinel}


def _accept(world, *titles):
    decisions = [Decision(f"d{i}", "accept", (world["groups"][t].id,), None, datetime(2026, 1, 1, tzinfo=UTC))
                 for i, t in enumerate(titles)]
    run = propose_groups(world["inventory"], decisions=decisions)
    return run, {e.group_id: e for e in assess_run(run, world["inventory"])}


def _approve(world, title="SpamFilter", **kwargs):
    run, results = _accept(world, title)
    group = run.group(world["groups"][title].id)
    proposal = propose_selection(group, world["inventory"], PrivacySettings())
    return approve_files(inventory_key="inv", group=group, eligibility=results[group.id], proposal=proposal,
                         inventory=world["inventory"], **kwargs), group


def _pack(world):
    approval, group = _approve(world)
    reader = ApprovedFileReader(approval, PrivacySettings())
    return build_evidence_pack(approval, reader), approval, reader


# --- eligibility (metadata only) -------------------------------------------------------


def test_eligibility_separates_projects_from_personal_documents(world):
    results = {e.group_id: e for e in assess_run(world["run"], world["inventory"])}
    g = world["groups"]
    assert results[g["SpamFilter"].id].status is EligibilityStatus.TECHNICAL_CANDIDATE
    assert results[g["Personal"].id].status is EligibilityStatus.PERSONAL_OR_ADMIN


def test_uncertain_groups_need_review_and_labels_exclude():
    inv = make_inventory({"Downloads/notes.txt": 1, "Downloads/helper.py": 90, "Downloads/budget.xlsx": 200,
                          "Projects/somelib/a.py": 1, "Projects/somelib/b.py": 2})
    run = propose_groups(inv)
    by_title = {g.working_title.text: g for g in run.groups}
    results = {e.group_id: e for e in assess_run(run, inv)}
    loose = next(g for g in run.groups if g.kind.value == "loose_collection")
    assert results[loose.id].status is EligibilityStatus.NEEDS_REVIEW
    lib = by_title["somelib"]
    labelled = replace(run, groups=tuple(replace(g, labels=("third_party",)) if g.id == lib.id else g
                                         for g in run.groups))
    assert {e.group_id: e for e in assess_run(labelled, inv)}[lib.id].status is EligibilityStatus.EXCLUDED


def test_eligibility_reads_no_files(world, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("eligibility must not open or list files")
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(os, "scandir", forbidden)
    results = assess_run(world["run"], world["inventory"])
    monkeypatch.undo()
    assert results


def test_eligibility_never_sets_ownership_labels(world):
    pack, approval, _ = _pack(world)
    run, _ = _accept(world, "SpamFilter")
    assert run.group(approval.group_id).labels == ()


def _statuses(files, **kwargs):
    inv = make_inventory(files, **kwargs)
    run = propose_groups(inv)
    results = {e.group_id: e for e in assess_run(run, inv)}
    return {g.working_title.text: results[g.id] for g in run.groups}, run, inv


def test_single_file_assignment_is_a_technical_candidate():
    statuses, _, _ = _statuses({"Course CS 150/hw4.py": 1, "Course CS 150/hw5.py": 9})
    assert statuses["hw 4"].status is EligibilityStatus.TECHNICAL_CANDIDATE
    assert statuses["hw 4"].code_files == 1 and statuses["hw 4"].total_files == 1


def test_single_notebook_project_is_eligible():
    statuses, _, _ = _statuses({"Projects/Data Viz Project/explore.ipynb": 1})
    assert statuses["Data Viz Project"].status is EligibilityStatus.TECHNICAL_CANDIDATE
    assert statuses["Data Viz Project"].notebook_files == 1


def test_document_heavy_technical_project_needs_review_and_can_be_approved():
    files = {"Projects/Robotics Project/final report.pdf": 1, "Projects/Robotics Project/slides.pptx": 1,
             "Projects/Robotics Project/design notes.docx": 1, "Projects/Robotics Project/controller.py": 1}
    statuses, run, inv = _statuses(files)
    robotics = statuses["Robotics Project"]
    assert robotics.status is EligibilityStatus.NEEDS_REVIEW
    assert any("only 1 of 4 files" in r for r in robotics.reasons)
    group = next(g for g in run.groups if g.working_title.text == "Robotics Project")
    accepted = replace(group, status=GroupStatus.ACCEPTED)
    proposal = propose_selection(accepted, inv, PrivacySettings())
    assert [s.project_path for s in proposal.selected] == ["controller.py"]
    with pytest.raises(ApprovalError, match="needs review"):
        approve_files(inventory_key="inv", group=accepted, eligibility=robotics, proposal=proposal, inventory=inv)
    record = approve_files(inventory_key="inv", group=accepted, eligibility=robotics, proposal=proposal,
                           inventory=inv, acknowledge_review=True)
    assert [f.project_path for f in record.files] == ["controller.py"]


def test_documents_only_project_needs_review_not_personal():
    statuses, run, inv = _statuses({"Projects/Bridge Design/final report.pdf": 1,
                                    "Projects/Bridge Design/presentation.pptx": 2})
    bridge = statuses["Bridge Design"]
    assert bridge.status is EligibilityStatus.NEEDS_REVIEW
    group = replace(next(g for g in run.groups if g.working_title.text == "Bridge Design"),
                    status=GroupStatus.ACCEPTED)
    with pytest.raises(ApprovalError, match="No analyzable files"):  # documents are not read in this version
        approve_files(inventory_key="inv", group=group, eligibility=bridge,
                      proposal=propose_selection(group, inv, PrivacySettings()), inventory=inv,
                      acknowledge_review=True)


def test_genuinely_personal_documents_are_personal_or_admin():
    statuses, _, _ = _statuses({"Paperwork/resume 2024.docx": 1, "Paperwork/tax return.pdf": 2,
                                "Bank/bank statement march.pdf": 3, "Bank/bank statement april.pdf": 4})
    assert statuses["Paperwork"].status is EligibilityStatus.PERSONAL_OR_ADMIN
    assert statuses["Bank"].status is EligibilityStatus.PERSONAL_OR_ADMIN
    assert any("resume" in r or "tax" in r for r in statuses["Paperwork"].reasons)


def test_personal_names_next_to_code_need_review_rather_than_exclusion():
    statuses, _, _ = _statuses({"Projects/Job Tools/resume_parser.py": 1, "Projects/Job Tools/parse_test.py": 2})
    tools = statuses["Job Tools"]
    assert tools.status is EligibilityStatus.NEEDS_REVIEW
    assert any("resume" in r for r in tools.reasons)


def test_no_group_is_ever_approved_by_eligibility(world):
    run = world["run"]
    for e in assess_run(run, world["inventory"]):
        assert e.status is not EligibilityStatus.TECHNICAL_CANDIDATE or run.group(e.group_id).status \
            is GroupStatus.PROPOSED  # still unaccepted, so nothing can be read yet


# --- selection and approval (Gate 1) ---------------------------------------------------


def test_selection_includes_supported_files_and_explains_exclusions(world):
    run, _ = _accept(world, "SpamFilter")
    proposal = propose_selection(run.group(world["groups"]["SpamFilter"].id), world["inventory"], PrivacySettings())
    assert {s.project_path for s in proposal.selected} == {
        "train.py", "explore.ipynb", "queries.sql", "README.md", "requirements.txt"}
    assert {(x.project_path, x.reason.split(" (")[0]) for x in proposal.excluded} == {("data/messages.csv", "data file")}


def test_sensitive_and_private_files_are_never_selectable():
    inv = make_inventory({"Projects/App/main.py": 1, "Projects/App/secrets.py": 1, "Projects/App/Taxes/calc.py": 1},
                         marker_files={"home/Projects/App": ["requirements.txt"]})
    run = propose_groups(inv)
    group = next(g for g in run.groups if g.working_title.text == "App")
    proposal = propose_selection(group, inv, PrivacySettings())
    assert [s.project_path for s in proposal.selected] == ["main.py"]
    reasons = {x.project_path: x.reason for x in proposal.excluded}
    assert reasons == {"[redacted]": "sensitive file name", "Taxes/calc.py": "inside a private folder"}


def test_approval_requires_an_accepted_eligible_group(world):
    results = {e.group_id: e for e in assess_run(world["run"], world["inventory"])}
    spam = world["groups"]["SpamFilter"]
    proposal = propose_selection(spam, world["inventory"], PrivacySettings())
    with pytest.raises(ApprovalError, match="Accept the group"):
        approve_files(inventory_key="inv", group=spam, eligibility=results[spam.id], proposal=proposal,
                      inventory=world["inventory"])
    run, results = _accept(world, "Personal")
    personal = run.group(world["groups"]["Personal"].id)
    with pytest.raises(ApprovalError, match="personal_or_admin"):
        approve_files(inventory_key="inv", group=personal, eligibility=results[personal.id],
                      proposal=propose_selection(personal, world["inventory"], PrivacySettings()),
                      inventory=world["inventory"])


def test_needs_review_requires_explicit_acknowledgement(world):
    approval, group = _approve(world)
    run, results = _accept(world, "SpamFilter")
    review = replace(results[group.id], status=EligibilityStatus.NEEDS_REVIEW)
    proposal = propose_selection(run.group(group.id), world["inventory"], PrivacySettings())
    with pytest.raises(ApprovalError, match="needs review"):
        approve_files(inventory_key="inv", group=run.group(group.id), eligibility=review, proposal=proposal,
                      inventory=world["inventory"])
    record = approve_files(inventory_key="inv", group=run.group(group.id), eligibility=review, proposal=proposal,
                           inventory=world["inventory"], acknowledge_review=True)
    assert record.acknowledged_review


def test_only_selectable_files_can_be_approved(world):
    data_file = next(f.id for f in world["inventory"].files if f.name == "messages.csv")
    with pytest.raises(ApprovalError, match="not in this group's selectable files"):
        _approve(world, file_ids=[data_file])
    approval, _ = _approve(world, file_ids=[next(f.id for f in world["inventory"].files if f.name == "train.py")])
    assert [f.project_path for f in approval.files] == ["train.py"]


# --- reading only approved files -------------------------------------------------------


def test_only_approved_files_are_opened_and_code_never_runs(world, monkeypatch):
    approval, _ = _approve(world)
    opened: list[str] = []
    real_open = builtins.open

    def tracking_open(file, *args, **kwargs):
        opened.append(os.path.abspath(os.fspath(file)))
        return real_open(file, *args, **kwargs)

    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in world["root"].rglob("*") if p.is_file()}
    monkeypatch.setattr(builtins, "open", tracking_open)
    pack = build_evidence_pack(approval, ApprovedFileReader(approval, PrivacySettings()))
    monkeypatch.undo()
    assert sorted(opened) == sorted(os.path.abspath(f.absolute_path) for f in approval.files)
    assert not world["sentinel"].exists()  # the script's side effect never happened
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in world["root"].rglob("*") if p.is_file()} == before
    assert pack.items


def test_unapproved_and_changed_files_are_refused(world):
    approval, _ = _approve(world)
    reader = ApprovedFileReader(approval, PrivacySettings())
    other = next(f.id for f in world["inventory"].files if f.name == "resume.docx")
    with pytest.raises(ReadRefused):
        reader.read_text(other)
    target = next(f for f in approval.files if f.project_path == "README.md")
    Path(target.absolute_path).write_text("# Changed after approval\n", encoding="utf-8")
    with pytest.raises(ReadRefused, match="changed since it was approved"):
        reader.read_text(target.file_id)


def test_links_are_never_read(world, tmp_path):
    approval, _ = _approve(world)
    target = next(f for f in approval.files if f.project_path == "README.md")
    os.remove(target.absolute_path)
    try:
        os.symlink(tmp_path / "elsewhere.md", target.absolute_path)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symlinks")
    with pytest.raises(ReadRefused):
        ApprovedFileReader(approval, PrivacySettings()).read_text(target.file_id)


def test_read_refusals_never_include_the_path(world):
    approval, _ = _approve(world)
    target = next(f for f in approval.files if f.project_path == "README.md")
    os.remove(target.absolute_path)
    with pytest.raises(ReadRefused) as excinfo:
        ApprovedFileReader(approval, PrivacySettings()).read_text(target.file_id)
    assert str(world["root"]) not in str(excinfo.value)


# --- evidence extraction ------------------------------------------------------------


def _items(pack, path):
    return [i for i in pack.items if i.project_path == path]


def test_python_evidence_with_source_references(world):
    pack, _, _ = _pack(world)
    py = _items(pack, "train.py")
    kinds = {(i.kind, i.text.splitlines()[0]) for i in py}
    assert ("docstring", "Spam filter training script.") in kinds
    assert ("import", "import numpy as np") in kinds
    assert any(k == "definition" and t.startswith("def train(messages") for k, t in kinds)
    assert ("definition", "class SpamModel:") in kinds
    definition = next(i for i in py if i.text.startswith("def train"))
    source_lines = (world["root"] / "Projects/SpamFilter/train.py").read_text(encoding="utf-8").splitlines()
    expected_line = next(n for n, line in enumerate(source_lines, start=1) if line.startswith("def train"))
    assert definition.line_start == expected_line and "naive Bayes" in definition.text
    techs = {t.name: t.evidence_ids for t in pack.technologies}
    assert {"NumPy", "scikit-learn", "Python"} <= set(techs)
    ids = {i.id for i in pack.items}
    assert all(e in ids for refs in techs.values() for e in refs)


def test_notebook_evidence_excludes_outputs(world):
    pack, _, _ = _pack(world)
    nb = _items(pack, "explore.ipynb")
    assert {i.cell for i in nb} == {1, 2, 3}
    assert any(i.kind == "markdown" and "Exploring message lengths" in i.text for i in nb)
    assert any(i.kind == "definition" and i.text.startswith("def plot_lengths") for i in nb)
    assert "SECRET OUTPUT VALUE" not in json.dumps([i.text for i in pack.items])
    assert any("output(s) excluded" in w for w in pack.warnings)
    assert "pandas" in {t.name for t in pack.technologies}


def test_sql_readme_and_requirements_evidence(world):
    pack, _, _ = _pack(world)
    sql = [i.text for i in _items(pack, "queries.sql")]
    assert any(t.startswith("CREATE TABLE messages") for t in sql)
    assert any("tables: labels, messages" in t for t in sql)
    assert any(i.kind == "readme" and i.text == "# Spam Filter" for i in _items(pack, "README.md"))
    deps = [i.text for i in _items(pack, "requirements.txt")]
    assert deps == ["numpy>=1.24", "scikit-learn==1.3"]


def test_java_and_r_evidence():
    from portfolioforge.understanding.extract import java_evidence, r_evidence
    java = java_evidence("package demo;\nimport javax.swing.JFrame;\npublic class Main {\n"
                         "    public static void main(String[] args) {\n    }\n}\n")
    assert [(e.kind, e.line_start) for e in java] == [("statement", 1), ("import", 2), ("definition", 3),
                                                       ("definition", 4)]
    assert java[1].module == "javax.swing.JFrame"
    r = r_evidence("#' Fit a model\nlibrary(ggplot2)\nfit_model <- function(df) {\n  stats::lm(y ~ x, df)\n}\n")
    assert {(e.kind, e.module) for e in r} >= {("docstring", None), ("import", "ggplot2"), ("definition", None),
                                               ("import", "stats")}
    rmd = r_evidence("# Results\n\n```{r setup}\nlibrary(dplyr)\n```\nplain text\n", markdown=True)
    assert [(e.kind, e.text) for e in rmd][:3] == [("markdown", "# Results"), ("chunk", "```{r setup}"),
                                                   ("import", "library(dplyr)")]


def test_invalid_python_falls_back_without_running_anything(tmp_path):
    from portfolioforge.understanding.extract import python_evidence
    items = python_evidence("import os\ndef broken(:\n    pass\n")
    assert [(e.kind, e.line_start) for e in items] == [("import", 1), ("definition", 2)]


def test_every_item_has_a_reference_and_a_matching_hash(world):
    import hashlib
    pack, _, _ = _pack(world)
    assert len({i.id for i in pack.items}) == len(pack.items)
    for item in pack.items:
        assert item.project_path and item.line_start >= 1 and item.line_end >= item.line_start
        assert item.sha256 == hashlib.sha256(item.text.encode()).hexdigest()


# --- redaction -----------------------------------------------------------------------------


def test_personal_details_are_redacted_from_evidence(world):
    pack, _, _ = _pack(world)
    text = json.dumps([i.text for i in pack.items])
    for secret in ("Example Student", "example.student@example.org", "caseywren", FAKE_SECRET, str(world["root"])):
        assert secret not in text
    totals: dict[str, int] = {}
    for item in pack.items:
        for kind, n in item.redactions.items():
            totals[kind] = totals.get(kind, 0) + n
    assert {"name", "secret", "path"} <= set(totals)


@pytest.mark.parametrize(("text", "kind"), [
    ("# Author: Pat Example", "name"),
    ("// @author Pat Example", "name"),
    ("# Name: Pat Example", "name"),
    ("contact me at pat@example.org", "email"),
    ("call (555) 123-4567", "phone"),
    ("pass" + "word = 'hunter2hunter2'", "secret"),
    ("data = '" + "x" * 250 + "'", "long_literal"),
])
def test_redaction_rules(text, kind):
    cleaned, counts = redact(text)
    assert counts.get(kind) == 1 and cleaned != text


def test_ordinary_code_is_not_redacted():
    code = ("name = input('Your name: ')\nemail_count = 3\nstudent_scores = []\n"
            "token = tokenizer.tokenize(text)\nkey = sorted(items)[0]\nTOKEN_LIMIT=$LIMIT\n")
    assert redact(code) == (code, {})


def test_unquoted_environment_secrets_are_redacted():
    value = "abc" + "DEF123xyz"
    cleaned, counts = redact(f"export API_TOKEN={value} && python app.py")
    assert value not in cleaned and "API_TOKEN=[secret removed]" in cleaned and counts == {"secret": 1}


# --- disclosure manifest and enforcement (Gate 2) ---------------------------------------


def test_manifest_shows_exactly_what_would_be_sent(world):
    pack, _, _ = _pack(world)
    manifest, payload = build_manifest(pack, "dry-run", "dry-run")
    assert manifest.payload_bytes == len(payload)
    assert manifest.estimated_tokens == -(-len(payload) // 4)
    sent = json.loads(payload)
    assert {e["id"] for e in sent["evidence"]} == {i.evidence_id for i in manifest.items}
    raw = payload.decode()
    assert str(world["root"]) not in raw and "Projects" not in raw and "SpamFilter" not in raw.split('"path"')[0]
    assert "2026" not in raw and "modified" not in raw
    assert any("notebook cell outputs" in line for line in manifest.not_sent)
    assert build_manifest(pack, "dry-run", "dry-run")[1] == payload  # deterministic


def test_nothing_is_sent_without_matching_approval(world):
    pack, _, _ = _pack(world)
    manifest, payload = build_manifest(pack, "dry-run", "dry-run")
    provider = DryRunProvider()
    with pytest.raises(DisclosureNotApproved):
        submit(manifest, payload, None, provider)
    approval = approve_manifest(manifest)
    with pytest.raises(DisclosureNotApproved, match="payload differs"):
        submit(manifest, payload + b" ", approval, provider)
    other_model, _ = build_manifest(pack, "dry-run", "other-model")
    with pytest.raises(DisclosureNotApproved):
        submit(other_model, payload, approval, DryRunProvider("other-model"))
    assert provider.received == []


def test_dry_run_sends_nothing_externally(world, monkeypatch):
    pack, _, _ = _pack(world)
    manifest, payload = build_manifest(pack, "dry-run", "dry-run")
    approval = approve_manifest(manifest)

    def no_network(*a, **k):
        raise AssertionError("no network access allowed")
    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    provider = DryRunProvider()
    result = submit(manifest, payload, approval, provider)
    assert result.sent is False and result.status == "dry_run_not_sent"
    assert provider.received == [payload]


@pytest.mark.parametrize("name", [None, "", "openai", "claude", "Anthropic"])
def test_there_is_no_default_provider_and_unknown_names_fail(name):
    with pytest.raises(ProviderNotConfigured):
        get_provider(name)


# --- local storage --------------------------------------------------------------------------


def test_records_are_stored_outside_git(world, tmp_path, monkeypatch):
    from portfolioforge.understanding.store import save_file_approval, save_pack
    approval, _ = _approve(world)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(repo / "data"))
    with pytest.raises(paths.UnsafeOutputLocation):
        save_file_approval(approval)
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "pf-data"))
    path = save_file_approval(approval)
    assert path.is_relative_to(tmp_path / "pf-data" / "understanding")
    pack = build_evidence_pack(approval, ApprovedFileReader(approval, PrivacySettings()))
    assert save_pack(pack).is_relative_to(tmp_path / "pf-data")


@pytest.mark.parametrize("bad", ["..", "a/b", "a\\b", "x:y", ""])
def test_storage_rejects_unsafe_identifiers(bad):
    from portfolioforge.understanding.store import group_dir
    with pytest.raises(ValueError):
        group_dir(bad, "g_1")
