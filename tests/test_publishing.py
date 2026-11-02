"""Publishing workflow. Invented projects only; the GitHub CLI is always faked.

Git runs for real, on temporary staging folders. Nothing here can reach GitHub: any attempt to
start the real GitHub CLI fails the test.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import make_tree
from portfolioforge import paths
from portfolioforge.grouping import cli as gcli
from portfolioforge.grouping.store import load_run
from portfolioforge.inventory import JsonlSink, plan_targeted, run_scan
from portfolioforge.publishing import cli as pcli
from portfolioforge.publishing import workflow as wf
from portfolioforge.understanding.analysis import accept_proposal
from portfolioforge.understanding.models import CommandFinding, LocalFindings, ValidationResult, ValidationStep
from portfolioforge.understanding.store import save_proposal
from test_build_inspection import (
    FAKE_EMAIL,
    FAKE_PASSWORD,
    FAKE_TOKEN,
    PASSWORD_WORD,
    app_source,
    netbeans_files,
    proposal_for,
    tree_hash,
)

KEY = "20260301T000000Z"
DECLARED = ("--ownership", "mine", "--course-policy", "not-coursework", "--license", "none")
SOURCES = ("src/com/example/shelf/ShelfApp.java", "src/com/example/shelf/Shelf.java")


@pytest.fixture(autouse=True)
def real_github_cli_is_forbidden(monkeypatch):
    real = subprocess.run

    def guarded(argv, *args, **kwargs):
        assert Path(argv[0]).stem != "gh", "the real GitHub CLI was started"
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded)
    for name in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{name}_NAME", "Synthetic Person")
        monkeypatch.setenv(f"GIT_{name}_EMAIL", "synthetic@users.noreply.github.com")


class FakeGitHub:
    """Stands in for `gh`. Git commands really run."""

    def __init__(self, login="synth-user", existing=(), view_error=None, create_code=0, reported=None):
        self.login, self.existing, self.view_error = login, set(existing), view_error
        self.create_code, self.reported = create_code, reported
        self.created: list[SimpleNamespace] = []

    def __call__(self, argv, cwd):
        if list(argv)[:3] == ["git", "config", "user.email"]:  # never this computer's real address
            return wf.ToolResult(0, "synthetic@users.noreply.github.com\n", "")
        if argv[0] != "gh":
            return wf.run_tool(argv, cwd)
        if argv[1] == "api":
            return wf.ToolResult(0, self.login + "\n", "")
        if argv[:2] == ["gh", "repo"] and argv[2] == "view":
            name = argv[3].split("/")[1]
            if "url,visibility" in argv:
                visibility = self.reported or self.created[-1].visibility
                return wf.ToolResult(0, json.dumps({"url": f"https://example.invalid/{name}", "visibility": visibility}), "")
            if self.view_error:
                return wf.ToolResult(1, "", self.view_error)
            if name in self.existing:
                return wf.ToolResult(0, json.dumps({"name": name}), "")
            return wf.ToolResult(1, "", "GraphQL: Could not resolve to a Repository with the name")
        if argv[2] == "create":
            if self.create_code:
                return wf.ToolResult(self.create_code, "", "failed")
            clean = {k: v for k, v in os.environ.items() if k != "GIT_DIR"}
            committed = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=cwd, capture_output=True,
                                       text=True, check=True, env=clean).stdout.split()
            message = subprocess.run(["git", "log", "--format=%B"], cwd=cwd, capture_output=True, text=True,
                                     check=True, env=clean).stdout.strip()
            self.created.append(SimpleNamespace(argv=list(argv), name=argv[3], committed=committed, message=message,
                                                visibility="PRIVATE" if "--private" in argv else "PUBLIC"))
            return wf.ToolResult(0, "", "")
        raise AssertionError(f"unexpected gh call: {argv}")


def _project(tmp_path, monkeypatch, fake_system, files, title, binary=None):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "pf-data"))
    root = make_tree(tmp_path / "Synthetic", files)
    for relative, data in (binary or {}).items():
        (root / relative).write_bytes(data)
    with JsonlSink(paths.inventories_dir() / KEY) as sink:
        run_scan(plan_targeted([root], system=fake_system), sink=sink)
    assert gcli.main(["propose"]) == 0
    group = next(g.id for g in load_run(KEY).groups if g.working_title.text == title)
    assert gcli.main(["accept", group]) == 0
    return root, group


def _accepted(group, **answer):
    proposal = accept_proposal(proposal_for(group, key=KEY, **answer), "shelf-tracker", acknowledge_flags=True,
                               taken_names=set())
    save_proposal(proposal)
    return proposal


@pytest.fixture
def pub(tmp_path, monkeypatch, fake_system, capsys):
    files = netbeans_files()
    files["Work/ShelfTracker/src/com/example/shelf/ShelfApp.java"] = app_source()
    root, group = _project(tmp_path, monkeypatch, fake_system, files, "ShelfTracker")
    proposal = _accepted(group)
    capsys.readouterr()
    github = FakeGitHub()
    monkeypatch.setattr(pcli, "make_runner", github)

    def run(*args, answer=None):
        kwargs = {"input_fn": lambda prompt: answer} if answer is not None else {}
        code = pcli.main(list(args), **kwargs)
        out = capsys.readouterr()
        return code, out.out, out.err

    def ids(*names):
        _, out, _ = run("candidates", group)
        found = dict(reversed(m) for m in re.findall(r"(f_\w+|bf_\w+)  (\S+)", out))
        return [item for name in names for item in ("--file", found[name])]

    def stage(*names, extra=DECLARED):
        code, out, err = run("stage", group, proposal.id, *ids(*(names or SOURCES)), *extra)
        assert code == 0, err
        return re.search(r"as (st_\w+)", out).group(1)

    return SimpleNamespace(root=root, group=group, proposal=proposal, github=github, run=run, ids=ids, stage=stage,
                           project=root / "Work" / "ShelfTracker")


def _staged(pub, stage_id):
    return wf.stage_dir(KEY, pub.group, stage_id)


def _approve(pub, stage_id, word="publish shelf-tracker"):
    code, out, err = pub.run("approve", pub.group, stage_id, answer=word)
    assert code == 0, out + err


# --- selection and staging ---------------------------------------------------------------------------


def test_nothing_is_included_without_naming_it(pub):
    code, _, err = pub.run("stage", pub.group, pub.proposal.id, *DECLARED)
    assert code == 1 and "explicitly" in err


def test_only_selected_files_and_generated_files_are_staged(pub):
    before = tree_hash(pub.root)
    stage_id = pub.stage(*SOURCES)
    root = _staged(pub, stage_id)
    assert set(tree_hash(root)) == {*SOURCES, "README.md", "PROVENANCE.md"}
    for path in SOURCES:
        assert (root / path).read_bytes() == (pub.project / path).read_bytes()  # copied unchanged
    assert tree_hash(pub.root) == before  # the original project is untouched
    assert pub.github.created == []  # staging contacts nothing
    assert paths.find_git_work_tree(root) is None


def test_credentials_and_private_files_cannot_be_selected(pub):
    _, out, _ = pub.run("candidates", pub.group)
    assert ".env" not in out and "private.properties" not in out and "config.properties" not in out
    code, _, err = pub.run("stage", pub.group, pub.proposal.id, "--file", "f_invented", *DECLARED)
    assert code == 1 and "not selectable" in err


def test_stage_needs_an_accepted_proposal(tmp_path, monkeypatch, fake_system, capsys):
    _, group = _project(tmp_path, monkeypatch, fake_system, netbeans_files(), "ShelfTracker")
    proposal = proposal_for(group, key=KEY)
    save_proposal(proposal)
    assert pcli.main(["stage", group, proposal.id, "--file", "x", *DECLARED]) == 1
    assert "Accept the proposal" in capsys.readouterr().err


def test_a_project_readme_is_not_silently_replaced(tmp_path, monkeypatch, fake_system, capsys):
    files = netbeans_files()
    files["Work/ShelfTracker/README.md"] = "# Original\n"
    _, group = _project(tmp_path, monkeypatch, fake_system, files, "ShelfTracker")
    proposal = _accepted(group)
    capsys.readouterr()
    assert pcli.main(["candidates", group]) == 0
    readme_id = re.search(r"(f_\w+)  README\.md", capsys.readouterr().out).group(1)
    assert pcli.main(["stage", group, proposal.id, "--file", readme_id, *DECLARED]) == 1
    assert "Deselect" in capsys.readouterr().err


def test_generated_files_are_clean_and_honest(pub):
    root = _staged(pub, pub.stage())
    readme = (root / "README.md").read_text(encoding="utf-8")
    provenance = (root / "PROVENANCE.md").read_text(encoding="utf-8")
    assert "Draft generated" not in readme
    for section in ("## Original material", "## Generated documentation", "## Changes made for publication",
                    "## Dependencies", "## Verified and unverified instructions"):
        assert section in provenance
    assert "Local compile check: **not executed**" in provenance and "Local run check: **not executed**" in provenance
    assert "copied unchanged" in provenance and "Ownership stated by the publisher: mine" in provenance
    for text in (readme, provenance):
        assert str(pub.root) not in text and "ShelfTracker/src" not in text and "Synthetic" not in text


def test_generated_readme_leak_blocks_and_cannot_be_accepted(tmp_path, monkeypatch, fake_system, capsys):
    files = netbeans_files()
    _, group = _project(tmp_path, monkeypatch, fake_system, files, "ShelfTracker")
    home = "C:" + "\\Users\\" + "qmarsh" + "\\work"
    proposal = _accepted(group)
    save_proposal(replace(proposal, user_summary=f"Lives in {home}"))
    capsys.readouterr()
    assert pcli.main(["candidates", group]) == 0
    ident = re.search(r"(f_\w+)  src/\S+Shelf\.java", capsys.readouterr().out).group(1)
    assert pcli.main(["stage", group, proposal.id, "--file", ident, *DECLARED]) == 0
    stage_id = re.search(r"as (st_\w+)", capsys.readouterr().out).group(1)
    findings = wf.run_checks(wf.load_stage(KEY, group, stage_id))
    leak = next(f for f in findings if f.kind == "generated_file_leak")
    assert leak.blocks and not leak.resolvable


# --- checks block ---------------------------------------------------------------------------------------


def _check_kinds(pub, stage_id):
    return {f.kind: f for f in wf.run_checks(wf.load_stage(KEY, pub.group, stage_id))}


@pytest.mark.parametrize("missing, kind", [
    (("--course-policy", "not-coursework", "--license", "none"), "unconfirmed_ownership"),
    (("--ownership", "mine", "--license", "none"), "unconfirmed_course_policy"),
    (("--ownership", "mine", "--course-policy", "not-coursework"), "license_not_chosen"),
    (("--ownership", "team", "--course-policy", "not-coursework", "--license", "none"), "attribution_missing"),
    (("--ownership", "mine", "--course-policy", "not-coursework", "--license", "MIT"), "license_file_missing"),
])
def test_undeclared_ownership_and_licensing_block(pub, missing, kind):
    stage_id = pub.stage(extra=missing)
    assert _check_kinds(pub, stage_id)[kind].blocks
    code, _, err = pub.run("approve", pub.group, stage_id, answer="publish shelf-tracker")
    assert code == 1 and "block publication" in err
    assert pub.github.created == []


def _with_source(tmp_path, monkeypatch, fake_system, capsys, source, name="Extra.java", **answer):
    files = netbeans_files()
    files[f"Work/ShelfTracker/src/com/example/shelf/{name}"] = source
    root, group = _project(tmp_path, monkeypatch, fake_system, files, "ShelfTracker")
    proposal = _accepted(group, **answer)
    capsys.readouterr()
    assert pcli.main(["candidates", group]) == 0
    ident = re.search(rf"(f_\w+)  src/\S+{re.escape(name)}", capsys.readouterr().out).group(1)
    return group, proposal, ident


def _stage_it(group, proposal, ident, capsys, *extra):
    assert pcli.main(["stage", group, proposal.id, "--file", ident, *DECLARED, *extra]) == 0
    return wf.load_stage(KEY, group, re.search(r"as (st_\w+)", capsys.readouterr().out).group(1))


def test_a_real_secret_blocks_and_cannot_be_accepted(tmp_path, monkeypatch, fake_system, capsys):
    group, proposal, ident = _with_source(tmp_path, monkeypatch, fake_system, capsys, f'class Extra {{ String t = "{FAKE_TOKEN}"; }}\n')
    stage = _stage_it(group, proposal, ident, capsys)
    findings = wf.run_checks(stage)
    token = next(f for f in findings if f.kind == "github_token")
    assert token.blocks and not token.resolvable
    with pytest.raises(wf.PublishRefused, match="cannot be accepted"):
        wf.resolve_finding(stage, findings, token.id, "it is fine")
    assert FAKE_TOKEN not in wf.format_preview(stage, findings, owner="x", name_free=True).split("----- README.md")[0]


def test_a_false_alarm_can_be_accepted_with_a_reason_only(tmp_path, monkeypatch, fake_system, capsys):
    group, proposal, ident = _with_source(tmp_path, monkeypatch, fake_system, capsys,
                                          f'class Extra {{ String {PASSWORD_WORD} = "{FAKE_PASSWORD}"; }}\n')
    stage = _stage_it(group, proposal, ident, capsys)
    credential = next(f for f in wf.run_checks(stage) if f.kind == "hardcoded_credential")
    assert credential.blocks and credential.resolvable
    with pytest.raises(wf.PublishRefused, match="reason"):
        wf.resolve_finding(stage, [credential], credential.id, "  ")
    updated = wf.resolve_finding(stage, [credential], credential.id, "invented demo value")
    assert next(f for f in wf.run_checks(updated) if f.kind == "hardcoded_credential").blocks is False


def test_a_file_changed_since_the_scan_cannot_be_staged(pub):
    (pub.project / "src/com/example/shelf/Shelf.java").write_text(
        f'package com.example.shelf;\nclass Shelf {{ String {PASSWORD_WORD} = "{FAKE_PASSWORD}"; }}\n')
    # the file changed after the scan, so the guarded reader refuses it until it is scanned again
    code, _, err = pub.run("stage", pub.group, pub.proposal.id, *pub.ids(SOURCES[1]), *DECLARED)
    assert code == 1 and "could not be read" in err


def test_third_party_notice_needs_attribution(tmp_path, monkeypatch, fake_system, capsys):
    notice = "/* Copyright (c) 2019 Invented Vendor. Licensed under the Apache License. */\nclass Extra {}\n"
    group, proposal, ident = _with_source(tmp_path, monkeypatch, fake_system, capsys, notice)
    assert next(f for f in wf.run_checks(_stage_it(group, proposal, ident, capsys)) if f.kind == "third_party_notice").blocks
    credited = _stage_it(group, proposal, ident, capsys, "--attribution", "Includes code from Invented Vendor.")
    assert not any(f.kind == "third_party_notice" for f in wf.run_checks(credited))
    root = wf.stage_dir(KEY, group, credited.id)
    assert "Includes code from Invented Vendor." in (root / "PROVENANCE.md").read_text(encoding="utf-8")


def test_ide_license_template_comment_is_not_treated_as_third_party(tmp_path, monkeypatch, fake_system, capsys):
    template = "/* To change this license header, choose License Headers in Project Properties. */\nclass Extra {}\n"
    group, proposal, ident = _with_source(tmp_path, monkeypatch, fake_system, capsys, template)
    assert not any(f.check == "attribution" for f in wf.run_checks(_stage_it(group, proposal, ident, capsys)))


def test_ai_attribution_concern_blocks_until_credited(tmp_path, monkeypatch, fake_system, capsys):
    group, proposal, ident = _with_source(tmp_path, monkeypatch, fake_system, capsys, "class Extra {}\n",
                                          attribution_concerns=["Looks like instructor starter code."])
    assert next(f for f in wf.run_checks(_stage_it(group, proposal, ident, capsys)) if f.kind == "ai_attribution_concern").blocks
    credited = _stage_it(group, proposal, ident, capsys, "--attribution", "Starter code by the course staff.")
    assert not any(f.kind == "ai_attribution_concern" for f in wf.run_checks(credited))


def test_identity_terms_block(tmp_path, monkeypatch, fake_system, capsys):
    group, proposal, ident = _with_source(tmp_path, monkeypatch, fake_system, capsys, "package edu.state.qmarsh;\nclass Extra {}\n")
    assert next(f for f in wf.run_checks(_stage_it(group, proposal, ident, capsys), identity_terms=["qmarsh"])
                if f.kind == "local_identity").blocks


def test_values_never_appear_in_findings(tmp_path, monkeypatch, fake_system, capsys):
    source = f'// {FAKE_EMAIL}\nclass Extra {{ String {PASSWORD_WORD} = "{FAKE_PASSWORD}"; }}\n'
    group, proposal, ident = _with_source(tmp_path, monkeypatch, fake_system, capsys, source)
    findings = wf.run_checks(_stage_it(group, proposal, ident, capsys))
    text = json.dumps([vars(f) for f in findings], default=str)
    assert FAKE_PASSWORD not in text and FAKE_EMAIL not in text


# --- preview and approval -----------------------------------------------------------------------------


def test_preview_shows_everything_and_uploads_nothing(pub):
    stage_id = pub.stage()
    code, out, _ = pub.run("preview", pub.group, stage_id)
    assert code == 0 and pub.github.created == []
    assert "synth-user/shelf-tracker (available)" in out and "Visibility:  PRIVATE" in out
    assert "----- README.md -----" in out and "----- PROVENANCE.md -----" in out and SOURCES[0] in out
    readme = (_staged(pub, stage_id) / "README.md").read_text(encoding="utf-8").rstrip()
    assert readme in out  # the exact text


def test_preview_redacted_shows_no_names(pub):
    code, out, _ = pub.run("--redact", "preview", pub.group, pub.stage())
    assert code == 0
    for name in ("shelf-tracker", "ShelfApp", "README.md", "synth-user", str(pub.root)):
        assert name not in out


def test_visibility_defaults_to_private_and_public_needs_its_own_phrase(pub):
    private = wf.load_stage(KEY, pub.group, pub.stage())
    assert private.visibility == "private"
    public_id = pub.stage(extra=(*DECLARED, "--visibility", "public"))
    code, out, _ = pub.run("approve", pub.group, public_id, answer="publish shelf-tracker")
    assert code == 1 and "Not approved" in out
    prompts = []
    assert pcli.main(["approve", pub.group, public_id],
                     input_fn=lambda p: prompts.append(p) or "publish public shelf-tracker") == 0
    assert "VISIBLE TO EVERYONE" in prompts[0]
    assert pub.run("publish", pub.group, public_id)[0] == 0
    assert "--public" in pub.github.created[0].argv and "--private" not in pub.github.created[0].argv


def test_approval_cannot_be_skipped(pub):
    stage_id = pub.stage()
    with pytest.raises(SystemExit):
        pcli.main(["approve", pub.group, stage_id, "--yes"])
    with pytest.raises(SystemExit):
        pcli.main(["publish", pub.group, stage_id, "--yes"])
    code, _, err = pub.run("publish", pub.group, stage_id)
    assert code == 1 and "no unused publication approval" in err
    for answer in ("yes", "no", "approve", "publish", "publish other-name", ""):
        code, out, _ = pub.run("approve", pub.group, stage_id, answer=answer)
        assert code == 1 and "Not approved" in out

    def nobody_there(prompt):
        raise EOFError

    assert pcli.main(["approve", pub.group, stage_id], input_fn=nobody_there) == 1
    assert pub.github.created == []


# --- publishing -----------------------------------------------------------------------------------------


def test_publish_creates_a_new_private_repository_with_exactly_the_staged_files(pub):
    stage_id = pub.stage()
    _approve(pub, stage_id)
    code, out, err = pub.run("publish", pub.group, stage_id)
    assert code == 0, err
    assert "new private repository" in out
    created = pub.github.created[0]
    assert created.name == "synth-user/shelf-tracker" and "--private" in created.argv
    assert sorted(created.committed) == sorted({*SOURCES, "README.md", "PROVENANCE.md"})
    assert created.message == "Initial publication"
    assert not (_staged(pub, stage_id) / ".git").exists()
    assert not any(a in created.argv for a in ("--force", "--clone"))


def test_an_approval_is_used_once(pub):
    stage_id = pub.stage()
    _approve(pub, stage_id)
    assert pub.run("publish", pub.group, stage_id)[0] == 0
    code, _, err = pub.run("publish", pub.group, stage_id)
    assert code == 1 and "no unused publication approval" in err
    assert len(pub.github.created) == 1
    audit = (paths.data_dir() / "understanding" / KEY / pub.group / "audit.jsonl").read_text()
    assert '"outcome": "published"' in audit and '"outcome": "refused"' in audit and "ShelfApp" not in audit


def test_existing_repository_is_never_overwritten(pub):
    pub.github.existing.add("shelf-tracker")
    stage_id = pub.stage()
    code, out, _ = pub.run("preview", pub.group, stage_id)
    assert code == 0 and "ALREADY EXISTS" in out
    code, _, err = pub.run("approve", pub.group, stage_id, answer="publish shelf-tracker")
    assert code == 1 and "never be overwritten" in err
    assert pub.github.created == []


def test_a_name_taken_after_approval_stops_publishing(pub):
    stage_id = pub.stage()
    _approve(pub, stage_id)
    pub.github.existing.add("shelf-tracker")
    code, _, err = pub.run("publish", pub.group, stage_id)
    assert code == 1 and "never be overwritten" in err and pub.github.created == []
    assert not (_staged(pub, stage_id) / ".git").exists()


def test_an_unclear_name_check_stops_everything(pub):
    stage_id = pub.stage()
    pub.github.view_error = "error connecting to api.github.com"
    code, _, err = pub.run("preview", pub.group, stage_id)
    assert code == 1 and "Could not check" in err


def test_an_extra_or_changed_staged_file_stops_publishing(pub):
    stage_id = pub.stage()
    _approve(pub, stage_id)
    root = _staged(pub, stage_id)
    (root / "report.json").write_text('{"files": []}')
    code, _, err = pub.run("publish", pub.group, stage_id)
    assert code == 1 and "different files" in err and pub.github.created == []
    (root / "report.json").unlink()

    other = pub.stage()
    _approve(pub, other)
    (_staged(pub, other) / "README.md").write_text("# edited after approval\n")
    code, _, err = pub.run("publish", pub.group, other)
    assert code == 1 and "changed after staging" in err and pub.github.created == []


def test_visibility_edited_in_the_record_is_refused(pub):
    stage_id = pub.stage()
    _approve(pub, stage_id)
    stage = wf.load_stage(KEY, pub.group, stage_id)
    wf.save_stage(replace(stage, visibility="public"))
    code, _, err = pub.run("publish", pub.group, stage_id)
    assert code == 1 and "differs from what was approved" in err and pub.github.created == []


def test_switching_github_account_after_approval_is_refused(pub):
    stage_id = pub.stage()
    _approve(pub, stage_id)
    pub.github.login = "another-account"
    code, _, err = pub.run("publish", pub.group, stage_id)
    assert code == 1 and "account differs" in err and pub.github.created == []


def test_declarations_edited_after_approval_are_refused(pub):
    stage = wf.load_stage(KEY, pub.group, pub.stage())
    approval = wf.approve(stage, [], "synth-user", True)
    with pytest.raises(wf.PublishRefused, match="changed after it was created"):
        wf.publish(replace(stage, ownership=None), approval, run=pub.github)
    assert pub.github.created == []


def test_publish_runs_the_checks_again(pub, monkeypatch):
    stage = wf.load_stage(KEY, pub.group, pub.stage())
    approval = wf.approve(stage, [], "synth-user", True)
    monkeypatch.setattr(wf, "run_checks", lambda *a, **k: [wf.Finding("x", "privacy", "new_rule", "high", None, None, "new")])
    with pytest.raises(wf.PublishRefused, match="block publication"):
        wf.publish(stage, approval, run=pub.github)
    assert pub.github.created == []


def test_a_data_folder_inside_a_repository_is_refused(pub, tmp_path):
    (tmp_path / "pf-data" / ".git").mkdir()
    code, _, err = pub.run("stage", pub.group, pub.proposal.id, *pub.ids(SOURCES[0]), *DECLARED)
    assert code == 1 and "inside a Git repository" in err


def test_public_commit_email_is_surfaced(pub):
    stage = wf.load_stage(KEY, pub.group, pub.stage(extra=(*DECLARED, "--visibility", "public")))
    kinds = {f.kind: f for f in wf.run_checks(stage, committer_email=FAKE_EMAIL)}
    assert kinds["public_commit_email"].blocks and kinds["no_license"].severity == "review"
    assert "public_commit_email" not in {f.kind for f in wf.run_checks(stage, committer_email="1+x@users.noreply.github.com")}
    assert FAKE_EMAIL not in kinds["public_commit_email"].detail


def test_a_failed_create_leaves_no_git_folder_and_says_what_to_check(pub):
    stage_id = pub.stage()
    _approve(pub, stage_id)
    pub.github.create_code = 1
    code, _, err = pub.run("publish", pub.group, stage_id)
    assert code == 1 and "could not create and push" in err and "Existing repositories were not changed" in err
    assert not (_staged(pub, stage_id) / ".git").exists()


def test_wrong_visibility_on_github_is_reported(pub):
    stage_id = pub.stage()
    _approve(pub, stage_id)
    pub.github.reported = "PUBLIC"
    code, _, err = pub.run("publish", pub.group, stage_id)
    assert code == 1 and "could not be confirmed" in err


def test_global_git_settings_cannot_change_what_is_published(pub, tmp_path, monkeypatch):
    ignore = tmp_path / "global-ignore"
    ignore.write_text("*.java\n")
    config = tmp_path / "global-config"
    config.write_text(f"[core]\n\texcludesFile = {ignore.as_posix()}\n\tautocrlf = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    stage_id = pub.stage()
    _approve(pub, stage_id)
    assert pub.run("publish", pub.group, stage_id)[0] == 0
    assert set(SOURCES) <= set(pub.github.created[0].committed)


def test_local_records_stay_out_of_the_repository(pub):
    stage_id = pub.stage()
    _approve(pub, stage_id)
    assert pub.run("publish", pub.group, stage_id)[0] == 0
    committed = pub.github.created[0].committed
    assert not any(re.search(r"inventor|evidence|manifest|proposal|approval|audit|publish-stage|\.env", p) for p in committed)


# --- any language ------------------------------------------------------------------------------------------


def test_a_project_with_no_inspector_publishes_the_same_way(tmp_path, monkeypatch, fake_system, capsys):
    notebook = json.dumps({"cells": [{"cell_type": "code", "source": ["print(1)\n"], "outputs": []}],
                           "metadata": {}, "nbformat": 4, "nbformat_minor": 5})
    root, group = _project(tmp_path, monkeypatch, fake_system, {
        "Work/SortLab/model.ipynb": notebook, "Work/SortLab/train.py": "print('hi')\n", "Work/SortLab/analysis.R": "x <- 1\n",
    }, "SortLab")
    proposal = _accepted(group)
    github = FakeGitHub()
    monkeypatch.setattr(pcli, "make_runner", github)
    capsys.readouterr()
    assert pcli.main(["candidates", group]) == 0
    ids = re.findall(r"(f_\w+)  ", capsys.readouterr().out)
    args = [a for i in ids for a in ("--file", i)]
    assert pcli.main(["stage", group, proposal.id, *args, *DECLARED]) == 0
    stage_id = re.search(r"as (st_\w+)", capsys.readouterr().out).group(1)
    assert not wf.blockers(wf.run_checks(wf.load_stage(KEY, group, stage_id)))  # a notebook alone does not block
    assert pcli.main(["approve", group, stage_id], input_fn=lambda p: "publish shelf-tracker") == 0
    assert pcli.main(["publish", group, stage_id]) == 0
    assert {"model.ipynb", "train.py", "analysis.R", "README.md", "PROVENANCE.md"} == set(github.created[0].committed)


def test_a_data_file_blocks_until_accepted(pub):
    stage_id = pub.stage(*SOURCES, "data/sample.csv")
    finding = _check_kinds(pub, stage_id)["data_file"]
    assert finding.blocks and finding.resolvable
    code, out, _ = pub.run("resolve", pub.group, stage_id, finding.id, "--reason", "invented sample", answer="no")
    assert code == 1 and "Not accepted" in out
    code, _, _ = pub.run("resolve", pub.group, stage_id, finding.id, "--reason", "invented sample", answer="accept")
    assert code == 0 and not _check_kinds(pub, stage_id)["data_file"].blocks


def test_workflow_names_no_language_or_build_tool():
    for module in (wf, pcli):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert not re.findall(r"(?i)\b(java|javac|maven|gradle|ant|netbeans|nbproject|jupyter)\b", source)


def _check(action, outcome, label):
    step = ValidationStep(label, (label,), 0, False, 0.1, "", "")
    return ValidationResult("vr", "vp", "bi", KEY, "g", action, None, outcome, {label: f"{label} 1.0"},
                            (step,) if label else (), (), True, datetime(2026, 2, 3, tzinfo=UTC))


def test_provenance_separates_a_supported_command_from_what_actually_ran(pub):
    command = CommandFinding("run", "buildtool run", "supported", "build_file", (), "defined")
    found = LocalFindings("bi", datetime(2026, 2, 3, tzinfo=UTC), (), (), (command,),
                          validations=(_check("compile", "compile_passed", "direct-tool"),))
    text = wf.provenance_markdown(replace(pub.proposal, local_findings=found), [], "private", "mine",
                                  "publishing-permitted", "none", None)
    assert "Run command in the README (`buildtool run`): supported by the project's files, but this command was " \
           "**not executed**" in text
    assert "Local compile check: executed `direct-tool` (direct-tool 1.0)" in text and "`compile_passed`" in text
    assert "Local run check: **not executed**" in text and "Tests: **not executed**" in text
    assert "coursework; the publisher states the course allows publishing it" in text
    assert "not part of the original project" in text


def test_provenance_does_not_count_a_check_that_could_not_start(pub):
    missing = replace(_check("compile", "tool_missing", "x"), steps=())
    found = LocalFindings("bi", datetime(2026, 2, 3, tzinfo=UTC), (), (), (), validations=(missing,))
    text = wf.provenance_markdown(replace(pub.proposal, local_findings=found), [], "private", "mine", None, None, None)
    assert "Local compile check: **not executed**" in text and "Origin: not confirmed" in text


def test_provenance_with_no_findings_claims_nothing(pub):
    text = wf.provenance_markdown(pub.proposal, [], "private", "mine", "not-coursework", "none", None)
    assert "None identified" in text and "no command is confirmed" in text and "Tests: **not executed**" in text


# --- review of the safety properties ------------------------------------------------------------------


@pytest.mark.parametrize("source, kind", [
    (f"// contact {FAKE_EMAIL}\nclass Extra {{}}\n", "email_address"),
    ('/** @author Quillon Marsh */\nclass Extra {}\n', "author_identity"),
    ('class Extra { String studentId = "S00123456"; }\n', "identifier"),
    ('class Extra { String dob = "2001-04-17"; }\n', "birth_date"),
    ('class Extra { String phone = "212-555-0134"; }\n', "phone_number"),
    ('class Extra { Object s = new Student("Quillon Marsh"); }\n', "person_name"),
])
def test_possible_personal_identifiers_block_until_accepted_by_name(tmp_path, monkeypatch, fake_system, capsys, source, kind):
    group, proposal, ident = _with_source(tmp_path, monkeypatch, fake_system, capsys, source)
    stage = _stage_it(group, proposal, ident, capsys)
    findings = wf.run_checks(stage)
    finding = next(f for f in findings if f.kind == kind)
    assert finding.blocks and finding.resolvable
    with pytest.raises(wf.PublishRefused, match="block publication"):
        wf.approve(stage, findings, "synth-user", True)
    updated = wf.resolve_finding(stage, findings, finding.id, "invented sample person")
    assert not next(f for f in wf.run_checks(updated) if f.kind == kind).blocks
    assert updated.resolutions[finding.id] == "invented sample person"


def test_identifier_in_a_file_path_blocks(tmp_path, monkeypatch, fake_system, capsys):
    group, proposal, ident = _with_source(tmp_path, monkeypatch, fake_system, capsys, "class Extra {}\n", name="qmarsh.java")
    findings = wf.run_checks(_stage_it(group, proposal, ident, capsys), identity_terms=["qmarsh"])
    assert any(f.kind == "identifier_in_path" and f.blocks for f in findings)


def _binary_project(tmp_path, monkeypatch, fake_system, capsys, name, data):
    _, group = _project(tmp_path, monkeypatch, fake_system, {"Work/SortLab/main.py": "print(1)\n"}, "SortLab",
                        binary={f"Work/SortLab/{name}": data})
    proposal = _accepted(group)
    capsys.readouterr()
    assert pcli.main(["candidates", group]) == 0
    ident = re.search(rf"(f_\w+)  {re.escape(name)}", capsys.readouterr().out).group(1)
    return group, proposal, ident


@pytest.mark.parametrize("name, data", [
    ("report.pdf", b"%PDF-1.4\n" + b"plain text then compressed \xff\xfe\x81 bytes\n"),  # no null byte
    ("notes.docx", b"PK\x03\x04\x00\x00binary"),
])
def test_files_that_cannot_be_scanned_block(tmp_path, monkeypatch, fake_system, capsys, name, data):
    group, proposal, ident = _binary_project(tmp_path, monkeypatch, fake_system, capsys, name, data)
    finding = next(f for f in wf.run_checks(_stage_it(group, proposal, ident, capsys)) if f.kind == "unscannable_file")
    assert finding.blocks and finding.resolvable


def test_notebook_outputs_and_long_lines_block(tmp_path, monkeypatch, fake_system, capsys):
    notebook = json.dumps({"cells": [{"cell_type": "code", "source": ["print(1)\n"],
                                      "outputs": [{"output_type": "stream", "text": ["id,name\n", "x" * 6000]}]}],
                           "metadata": {}, "nbformat": 4, "nbformat_minor": 5})
    root, group = _project(tmp_path, monkeypatch, fake_system, {"Work/SortLab/model.ipynb": notebook}, "SortLab")
    proposal = _accepted(group)
    capsys.readouterr()
    assert pcli.main(["candidates", group]) == 0
    ident = re.search(r"(f_\w+)  model\.ipynb", capsys.readouterr().out).group(1)
    kinds = {f.kind: f for f in wf.run_checks(_stage_it(group, proposal, ident, capsys))}
    assert kinds["notebook_outputs"].blocks and kinds["unscanned_long_line"].blocks


def test_approval_cannot_be_given_without_seeing_the_preview(pub):
    code, _, err = pub.run("--redact", "approve", pub.group, pub.stage(), answer="publish shelf-tracker")
    assert code == 1 and "--redact" in err


def test_a_file_changed_during_publish_is_caught_in_the_commit(pub):
    """Changed after every earlier check passed, just before git reads it."""
    stage_id = pub.stage()
    _approve(pub, stage_id)
    target = _staged(pub, stage_id) / SOURCES[0]
    github = pub.github

    def changes_it_first(argv, cwd):
        if "add" in argv:
            target.write_text("class Changed {}\n")
        return github(argv, cwd)

    pcli.make_runner = changes_it_first
    code, _, err = pub.run("publish", pub.group, stage_id)
    assert code == 1 and "commit differs from the approved files" in err
    assert github.created == [] and not (_staged(pub, stage_id) / ".git").exists()


def test_global_attributes_cannot_change_the_committed_bytes(pub, tmp_path, monkeypatch):
    attributes = tmp_path / "global-attributes"
    attributes.write_text("* text eol=lf\n*.java filter=evil\n")
    config = tmp_path / "global-config"
    config.write_text(f"[core]\n\tattributesFile = {attributes.as_posix()}\n[filter \"evil\"]\n\tclean = sed s/a/b/\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    stage_id = pub.stage()
    _approve(pub, stage_id)
    assert pub.run("publish", pub.group, stage_id)[0] == 0  # ignored, and verified against the approved bytes


@pytest.mark.parametrize("change", [{"attribution_concerns": ("added later",)}, {"course_policy": None}, {"license": "MIT"},
                                    {"repo_name": "other-name"}])
def test_any_edit_to_what_was_approved_is_refused(pub, change):
    stage = wf.load_stage(KEY, pub.group, pub.stage())
    approval = wf.approve(stage, [], "synth-user", True)
    edited = replace(stage, **change)
    with pytest.raises(wf.PublishRefused):
        wf.publish(edited, approval, run=pub.github)
    assert pub.github.created == []


def test_a_private_approval_cannot_publish_a_public_stage(pub):
    private = wf.load_stage(KEY, pub.group, pub.stage())
    public = wf.load_stage(KEY, pub.group, pub.stage(extra=(*DECLARED, "--visibility", "public")))
    approval = wf.approve(private, [], "synth-user", True)
    with pytest.raises(wf.PublishRefused, match="no unused publication approval"):
        wf.publish(public, approval, run=pub.github)
    with pytest.raises(wf.PublishRefused):
        wf.publish(public, replace(approval, stage_id=public.id), run=pub.github)
    assert pub.github.created == []


def test_public_email_is_rechecked_at_publish(pub, monkeypatch):
    stage_id = pub.stage(extra=(*DECLARED, "--visibility", "public"))
    stage = wf.load_stage(KEY, pub.group, stage_id)
    approval = wf.approve(stage, [], "synth-user", True)
    github = pub.github

    def personal_email(argv, cwd):
        if list(argv)[:3] == ["git", "config", "user.email"]:
            return wf.ToolResult(0, FAKE_EMAIL + "\n", "")
        return github(argv, cwd)

    with pytest.raises(wf.PublishRefused, match="block publication"):
        wf.publish(stage, approval, run=personal_email)
    assert github.created == []


def test_an_inherited_git_dir_is_ignored(pub, monkeypatch, tmp_path):
    other = tmp_path / "other-repo"
    subprocess.run(["git", "init", "--quiet", str(other)], check=True)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    stage_id = pub.stage()
    _approve(pub, stage_id)
    code, _, err = pub.run("publish", pub.group, stage_id)
    assert subprocess.run(["git", "--git-dir", str(other / ".git"), "rev-parse", "--verify", "HEAD"],
                          capture_output=True, env={k: v for k, v in os.environ.items() if k != "GIT_DIR"}).returncode != 0
    assert code == 0, err


def test_dotgit_paths_are_refused_in_any_case():
    for path in (".git/config", ".GIT/config", "src/.Git/hooks/pre-push", "../x", "-x", "C:/x", "a\\b"):
        assert not wf._safe_stage_path(path), path
    assert wf._safe_stage_path("src/Main.txt") and wf._safe_stage_path(".gitignore")
