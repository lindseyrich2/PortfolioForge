"""Batch orchestration. Invented projects only; the AI provider, GitHub CLI and code runner are all faked."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import socket
from types import SimpleNamespace

import pytest

from conftest import make_tree
from portfolioforge import paths
from portfolioforge.batch import cli as bcli
from portfolioforge.batch import workflow as bw
from portfolioforge.grouping import cli as gcli
from portfolioforge.inventory import JsonlSink, plan_targeted, run_scan
from portfolioforge.publishing import cli as pcli
from portfolioforge.publishing import workflow as pw
from portfolioforge.understanding import cli as ucli
from portfolioforge.understanding.analysis import evidence_from_anthropic_body
from portfolioforge.understanding.anthropic_provider import ProviderRequestFailed
from portfolioforge.understanding.models import ProviderResult
from portfolioforge.understanding.store import group_dir, list_proposals, list_validation_results
from test_build_inspection import FAKE_PASSWORD, PASSWORD_WORD, FakeRunner, _which, app_source, netbeans_files, tree_hash
from test_publishing import FakeGitHub, real_github_cli_is_forbidden  # noqa: F401 (autouse fixture)

KEY = "20260301T000000Z"
DECLARED = ("--ownership", "mine", "--license", "none")


class FakeProvider:
    name = "anthropic"

    def __init__(self, model, log, stop_on=None, fail_with=None, unusable=False):
        self.model, self.log, self.stop_on, self.fail_with, self.unusable = model, log, stop_on, fail_with, unusable

    def send(self, payload):
        body = json.loads(payload)
        evidence = evidence_from_anthropic_body(body)["evidence"]
        path = next(e["path"] for e in evidence if e["path"] != "local-inspection")
        self.log.append(hashlib.sha256(payload).hexdigest())
        if self.stop_on and self.stop_on in path:
            raise KeyboardInterrupt("the computer went to sleep mid-request")
        if self.fail_with:
            raise self.fail_with
        if self.unusable:
            return ProviderResult(self.name, self.model, True, len(payload), hashlib.sha256(payload).hexdigest(), "refusal", None)
        ids = [e["id"] for e in evidence[:1]]
        stem = re.sub(r"[^a-z]", "", path.rsplit("/", 1)[-1].rsplit(".", 1)[0].casefold()) or "tool"
        answer = {
            "title": f"Invented {stem}", "uncertainties": [], "attribution_concerns": [], "missing_information": [],
            "claims": [{"category": "purpose", "heading": "", "text": "Does a small invented task.", "evidence_ids": ids},
                       {"category": "summary", "heading": "", "text": "A small invented program.", "evidence_ids": ids}],
            "repository_names": [{"slug": f"invented-{stem}-tool", "rationale": "what it does", "evidence_ids": ids},
                                 {"slug": f"invented-{stem}-app", "rationale": "what it does", "evidence_ids": ids}],
            "steps": [], "compatibility_notes": [], "insufficient_evidence": False, "needs_local_inspection": False,
        }
        text = json.dumps(answer)
        return ProviderResult(self.name, self.model, True, len(payload), hashlib.sha256(payload).hexdigest(), "ok", text)


def _projects():
    files = {k.replace("Work/ShelfTracker", "Work/ShelfTracker"): v for k, v in netbeans_files().items()}
    files["Work/ShelfTracker/src/com/example/shelf/ShelfApp.java"] = app_source()
    files.update({
        "Work/SortLab/sorter.py": "def sort_items(items):\n    return sorted(items)\n",
        "Work/SortLab/requirements.txt": "numpy\n",
        "Work/SortLab/README.md": "# Sort lab\n",
        "Work/StatsLab/model.R": "fit_model <- function(x) {\n  x\n}\nlibrary(stats)\n",
        "Work/StatsLab/explore.ipynb": json.dumps({"cells": [{"cell_type": "code", "source": ["print(1)\n"], "outputs": []}],
                                                   "metadata": {}, "nbformat": 4, "nbformat_minor": 5}),
        "Work/LeakyTool/leaky.py": f'import os\n{PASSWORD_WORD} = "{FAKE_PASSWORD}"\n\n\ndef main():\n    return os.sep\n',
        "Work/ReportNotes/plot.py": "def plot():\n    return 1\n",
        "Work/ReportNotes/notes1.docx": "docx", "Work/ReportNotes/notes2.docx": "docx",
        "Work/ReportNotes/notes3.docx": "docx", "Work/ReportNotes/notes4.docx": "docx",
        "Personal/resume.docx": "docx",
        "Personal/tax return.pdf": "pdf",
    })
    return files


@pytest.fixture
def batch(tmp_path, monkeypatch, fake_system, capsys):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "pf-data"))
    root = make_tree(tmp_path / "Synthetic", _projects())
    with JsonlSink(paths.inventories_dir() / KEY) as sink:
        run_scan(plan_targeted([root], system=fake_system), sink=sink)
    assert gcli.main(["propose"]) == 0
    capsys.readouterr()
    provider_log: list[str] = []
    state = SimpleNamespace(stop_on=None, fail_with=None, unusable=False)
    monkeypatch.setattr(ucli, "make_provider", lambda name, model: FakeProvider(model, provider_log, state.stop_on,
                                                                                state.fail_with, state.unusable))
    github = FakeGitHub()
    monkeypatch.setattr(pcli, "make_runner", github)
    runner = FakeRunner(exit_codes=[0] * 20)
    monkeypatch.setattr(ucli, "make_runner", runner)
    monkeypatch.setattr(shutil, "which", lambda name: _which(name))

    def no_network(*args, **kwargs):
        raise AssertionError("the network was used")

    monkeypatch.setattr(socket.socket, "connect", no_network)

    def run(*args, answer=None, answers=None):
        if answers is not None:
            replies = iter(answers)
            kwargs = {"input_fn": lambda prompt: next(replies)}
        else:
            kwargs = {"input_fn": lambda prompt: answer if answer is not None else (_ for _ in ()).throw(AssertionError(prompt))}
        code = bcli.main(list(args), **kwargs)
        out = capsys.readouterr()
        return code, out.out, out.err

    def ids():
        queue = bw.load_queue(KEY)
        return {j.title: j.group_id for j in queue.jobs.values()}

    def job(title):
        return bw.load_queue(KEY).jobs[ids()[title]]

    return SimpleNamespace(root=root, run=run, ids=ids, job=job, log=provider_log, state=state, github=github,
                           runner=runner, tmp=tmp_path)


def _ready_to_send(batch, *titles):
    assert batch.run("run", "--provider", "anthropic")[0] == 0
    groups = [batch.ids()[t] for t in titles]
    code, _, err = batch.run("approve-read", *groups, "--acknowledge-review", answer=f"read {len(groups)}")
    assert code == 0, err
    return groups


def _proposed(batch, *titles):
    groups = _ready_to_send(batch, *titles)
    code, _, err = batch.run("send", *groups, answer=f"send {len(groups)} to anthropic")
    assert code == 0, err
    return groups


def _named(batch, *titles):
    groups = _proposed(batch, *titles)
    assert batch.run("accept-names", *groups, answer=f"accept {len(groups)}")[0] == 0
    return groups


def _staged(batch, *titles):
    groups = _named(batch, *titles)
    code, _, err = batch.run("stage", *groups, *DECLARED, answer=f"stage {len(groups)}")
    assert code == 0, err
    return groups


# --- run: safe, silent, and in priority order -------------------------------------------------------------


def test_run_never_asks_or_reads_or_sends(batch):
    code, out, _ = batch.run("run")  # the fixture fails if anything prompts
    assert code == 0 and "Nothing was sent" in out
    queue = bw.load_queue(KEY)
    for job in queue.jobs.values():
        assert not list(group_dir(KEY, job.group_id).glob("evidence-*")) if group_dir(KEY, job.group_id).is_dir() else True
    assert batch.log == [] and batch.github.created == []
    assert batch.job("SortLab").state == "needs_read_approval"


def test_likely_projects_come_first_and_personal_documents_are_set_aside(batch):
    batch.run("run")
    ordered = bw.load_queue(KEY).ordered()
    assert ordered[0].title == "ShelfTracker"  # most code, and a project
    assert batch.job("Personal").state == "not_eligible"
    assert ordered[-1].state in ("not_eligible", "needs_read_approval")
    code, _, err = batch.run("approve-read", batch.ids()["Personal"], answer="read 1")
    assert code == 1 and "Not at this step" in err
    assert not group_dir(KEY, batch.ids()["Personal"]).exists()


def test_read_approval_lists_files_and_needs_the_typed_count(batch):
    batch.run("run")
    groups = [batch.ids()["SortLab"], batch.ids()["StatsLab"]]
    code, out, _ = batch.run("approve-read", *groups, "--acknowledge-review", answer="read 1")
    assert code == 1 and "Nothing was read" in out and "sorter.py" in out and "model.R" in out
    assert batch.job("SortLab").state == "needs_read_approval"
    assert batch.run("approve-read", *groups, "--acknowledge-review", answer="read 2")[0] == 0
    assert batch.job("SortLab").state == "needs_provider"  # read and inspected; no provider chosen yet


def test_read_approval_respects_the_needs_review_gate(batch):
    batch.run("run")
    assert batch.job("ReportNotes").detail == "needs_review"
    held = batch.ids()["ReportNotes"]
    code, _, err = batch.run("approve-read", held, answer="read 1")
    assert code == 1 and "--acknowledge-review" in err
    assert batch.job("ReportNotes").state == "needs_read_approval"
    assert batch.run("approve-read", held, "--acknowledge-review", answer="read 1")[0] == 0


# --- disclosure ---------------------------------------------------------------------------------------------


def test_each_manifest_is_shown_and_nothing_is_sent_without_the_typed_phrase(batch):
    groups = _ready_to_send(batch, "SortLab", "StatsLab")
    assert batch.job("SortLab").state == "needs_disclosure_approval"
    assert batch.log == []  # preparing sends nothing
    for answer in ("yes", "send", "send 1 to anthropic", "send 2", ""):
        code, out, _ = batch.run("send", *groups, answer=answer)
        assert code == 1 and "Nothing was sent" in out
    assert batch.log == []
    assert out.count("Disclosure manifest") == 2 and "EXTERNAL" in out and "Payload SHA-256" in out and "an upper bound" in out


def test_a_group_of_manifests_is_sent_each_with_its_own_approval(batch):
    groups = _proposed(batch, "SortLab", "StatsLab")
    assert len(batch.log) == 2 and len(set(batch.log)) == 2
    for group in groups:
        assert len(list(group_dir(KEY, group).glob("disclosure-approval-*"))) == 1
        assert len(list_proposals(KEY, group)) == 1
    assert batch.job("SortLab").state == "needs_name"


def test_a_tampered_payload_is_refused_and_the_others_still_go(batch):
    groups = _ready_to_send(batch, "SortLab", "StatsLab")
    manifest_id = batch.job("SortLab").checkpoints["manifest_id"]
    payload = group_dir(KEY, batch.ids()["SortLab"]) / f"payload-{manifest_id}.json"
    payload.write_bytes(payload.read_bytes() + b" ")
    code, _, err = batch.run("send", *groups, answer="send 2 to anthropic")
    assert code == 1 and "differs from the approved manifest" in err
    assert len(batch.log) == 1  # only the untouched one left the computer
    assert batch.job("StatsLab").state == "needs_name" and batch.job("SortLab").state == "needs_disclosure_approval"


def test_only_projects_at_this_step_can_be_sent(batch):
    batch.run("run", "--provider", "anthropic")
    code, _, err = batch.run("send", batch.ids()["SortLab"], answer="send 1 to anthropic")
    assert code == 1 and "Not at this step" in err and batch.log == []


# --- interruption ------------------------------------------------------------------------------------------


def test_resume_repeats_nothing_that_succeeded(batch):
    _named(batch, "SortLab")
    before = len(batch.log)
    proposal_files = sorted(p.name for p in group_dir(KEY, batch.ids()["SortLab"]).glob("proposal-*"))
    for _ in range(3):
        assert batch.run("resume")[0] == 0
    assert len(batch.log) == before
    assert sorted(p.name for p in group_dir(KEY, batch.ids()["SortLab"]).glob("proposal-*")) == proposal_files
    assert batch.job("SortLab").state == "needs_staging"


def test_an_interrupted_send_resumes_without_a_silent_second_charge(batch):
    groups = _ready_to_send(batch, "SortLab", "StatsLab")
    batch.state.stop_on = "model.R"  # dies on the second project, mid-request
    order = sorted(groups, key=lambda g: g != batch.ids()["SortLab"])
    with pytest.raises(KeyboardInterrupt):
        batch.run("send", *order, answer="send 2 to anthropic")
    charged = len(batch.log)
    assert charged == 2
    batch.state.stop_on = None
    assert batch.run("resume")[0] == 0
    assert len(batch.log) == charged  # resume never sends
    assert batch.job("SortLab").state == "needs_name"  # the finished one kept its proposal
    assert batch.job("StatsLab").state == "needs_send_retry"
    stuck = batch.ids()["StatsLab"]
    code, _, err = batch.run("send", stuck, answer="send 1 to anthropic")
    assert code == 1 and "Not at this step" in err and len(batch.log) == charged
    code, out, _ = batch.run("send", stuck, "--resend", answer="send 1 to anthropic")
    assert code == 0 and "charged again" in out and len(batch.log) == charged + 1
    assert batch.job("StatsLab").state == "needs_name"


def test_an_answer_saved_before_an_interruption_is_not_paid_for_again(batch):
    groups = _ready_to_send(batch, "SortLab")
    job = batch.job("SortLab")
    manifest_id = job.checkpoints["manifest_id"]
    _, payload = bw.load_manifest(KEY, groups[0], manifest_id)
    answer = FakeProvider("m", []).send(payload).response
    (group_dir(KEY, groups[0]) / f"response-{manifest_id}-abc123.json").write_text(answer, encoding="utf-8")
    assert batch.run("resume")[0] == 0
    assert batch.log == [] and batch.job("SortLab").state == "needs_name"


def test_a_stop_part_way_through_a_run_keeps_what_was_done(batch, monkeypatch):
    real = bw.advance
    seen = []

    def stops_on_the_third(ctx, queue, job):
        if len(seen) == 2:
            raise KeyboardInterrupt
        seen.append(job.group_id)
        return real(ctx, queue, job)

    monkeypatch.setattr(bw, "advance", stops_on_the_third)
    with pytest.raises(KeyboardInterrupt):
        batch.run("run")
    saved = bw.load_queue(KEY).jobs
    assert all(saved[g].state != "new" for g in seen)
    assert sum(j.state == "new" for j in saved.values()) >= 1  # the rest had not started
    monkeypatch.setattr(bw, "advance", real)
    assert batch.run("resume")[0] == 0
    assert not any(j.state == "new" for j in bw.load_queue(KEY).jobs.values())


def _failed(message, status=None):
    return ProviderRequestFailed(message, status=status)


@pytest.mark.parametrize("failure", [
    _failed("Anthropic request failed: APITimeoutError during network transport (the request may have been sent)"),
    _failed("Anthropic request failed: APIConnectionError during network transport (the request may have been sent)"),
    _failed("Anthropic request failed: InternalServerError (HTTP 500, api_error)", 500),
    _failed("Anthropic request failed: APIStatusError (HTTP 529, overloaded_error)", 529),
    _failed("Anthropic request failed: APIStatusError (HTTP 408, timeout)", 408),
    _failed("Anthropic request failed: TypeError during response parsing (the request was sent and a response was received)"),
])
def test_ambiguous_failures_wait_for_you_and_are_never_retried(batch, failure):
    groups = _ready_to_send(batch, "SortLab")
    batch.state.fail_with = failure
    code, _, err = batch.run("send", *groups, answer="send 1 to anthropic")
    assert code == 1 and "Anthropic request failed" in err
    attempts = len(batch.log)
    assert attempts == 1
    batch.state.fail_with = None
    for command in ("resume", "run"):
        assert batch.run(command)[0] == 0
        assert len(batch.log) == attempts  # nothing resends it
        job = batch.job("SortLab")
        assert job.state == "needs_send_retry" and "doesn't show whether it was processed" in job.detail
    code, _, err = batch.run("send", *groups, answer="send 1 to anthropic")
    assert code == 1 and "Not at this step" in err and len(batch.log) == attempts
    _, out, _ = batch.run("review")
    assert "may have been processed or charged" in out and "send --resend" in out
    assert batch.run("send", *groups, "--resend", answer="send 1 to anthropic")[0] == 0  # only when you decide
    assert len(batch.log) == attempts + 1
    assert batch.job("SortLab").state == "needs_name" and "send_failure" not in batch.job("SortLab").checkpoints


@pytest.mark.parametrize("failure", [
    _failed("Anthropic request failed: BadRequestError (HTTP 400, invalid_request_error)", 400),
    _failed("Anthropic request failed: AuthenticationError (HTTP 401, authentication_error)", 401),
    _failed("Anthropic request failed: RateLimitError (HTTP 429, rate_limit_error)", 429),
    _failed("Anthropic request failed: TypeError during credential resolution (nothing was sent to Anthropic)"),
])
def test_a_failure_that_shows_it_was_never_accepted_can_be_sent_normally_after_your_decision(batch, failure):
    groups = _ready_to_send(batch, "SortLab")
    batch.state.fail_with = failure
    assert batch.run("send", *groups, answer="send 1 to anthropic")[0] == 1
    batch.state.fail_with = None
    assert batch.run("resume")[0] == 0
    assert len(batch.log) == 1  # still no automatic retry
    assert batch.job("SortLab").state == "needs_disclosure_approval"


def test_an_unusable_answer_is_treated_as_maybe_charged(batch):
    groups = _ready_to_send(batch, "SortLab")
    batch.state.unusable = True
    assert batch.run("send", *groups, answer="send 1 to anthropic")[0] == 1
    batch.state.unusable = False
    assert batch.run("resume")[0] == 0
    job = batch.job("SortLab")
    assert len(batch.log) == 1 and job.state == "needs_send_retry" and "may have charged" in job.detail


def test_one_ambiguous_failure_does_not_stop_the_other_sends(batch):
    groups = _ready_to_send(batch, "SortLab", "StatsLab")
    calls = []
    real = ucli.make_provider

    def fails_first(name, model):
        calls.append(name)
        if len(calls) == 1:
            batch.state.fail_with = _failed("Anthropic request failed: APITimeoutError during network transport")
        else:
            batch.state.fail_with = None
        return real(name, model)

    ucli.make_provider = fails_first
    assert batch.run("send", *groups, answer="send 2 to anthropic")[0] == 1
    states = bw.load_queue(KEY).jobs
    assert {states[g].state for g in groups} == {"needs_send_retry", "needs_name"}


def test_a_saved_answer_that_is_invalid_is_reported_without_a_new_request(batch):
    groups = _ready_to_send(batch, "SortLab")
    manifest_id = batch.job("SortLab").checkpoints["manifest_id"]
    (group_dir(KEY, groups[0]) / f"response-{manifest_id}-abc123.json").write_text("not json", encoding="utf-8")
    assert batch.run("resume")[0] == 0
    job = batch.job("SortLab")
    assert job.state == "error" and "InvalidModelOutput" in job.error and batch.log == []


# --- isolation and errors -------------------------------------------------------------------------------------


def test_one_project_failing_does_not_stop_the_others(batch, monkeypatch):
    batch.run("run")
    bad, good = batch.ids()["SortLab"], batch.ids()["StatsLab"]
    batch.run("approve-read", bad, good, "--acknowledge-review", answer="read 2")
    real = bw.advance

    def failing(ctx, queue, job):
        if job.group_id == bad:
            raise OSError("cannot read C:" + "\\Users\\" + "qmarsh" + "\\thing")
        return real(ctx, queue, job)

    monkeypatch.setattr(bw, "advance", failing)
    assert batch.run("resume")[0] == 0
    job = batch.job("SortLab")
    assert job.state == "error" and "OSError" in job.error and "qmarsh" not in job.error
    assert batch.job("StatsLab").state == "needs_provider"
    _, out, _ = batch.run("review")
    assert "ERROR" in out and "resume retries this" in out
    monkeypatch.setattr(bw, "advance", real)
    assert batch.run("resume")[0] == 0
    assert batch.job("SortLab").error is None


def test_a_damaged_queue_stops_and_is_not_overwritten(batch):
    batch.run("run")
    path = bw.queue_path(KEY)
    path.write_text("{not json", encoding="utf-8")
    code, _, err = batch.run("status")
    assert code == 1 and err
    assert path.read_text(encoding="utf-8") == "{not json"


def test_excluding_needs_a_reason(batch):
    batch.run("run")
    with pytest.raises(SystemExit):
        bcli.main(["exclude", batch.ids()["SortLab"]])
    code, _, err = batch.run("exclude", batch.ids()["SortLab"], "--reason", "  ")
    assert code == 1 and "reason" in err


@pytest.mark.parametrize("command, args, state", [("exclude", ("--reason", "a duplicate"), "excluded"), ("defer", (), "deferred")])
def test_set_aside_projects_are_not_read_or_sent_and_can_come_back(batch, command, args, state):
    batch.run("run", "--provider", "anthropic")
    group = batch.ids()["SortLab"]
    assert batch.run(command, group, *args)[0] == 0
    assert batch.job("SortLab").state == state
    assert not group_dir(KEY, group).exists()  # no approval, evidence, manifest or anything else
    assert batch.run("resume")[0] == 0
    assert batch.job("SortLab").state == state and not group_dir(KEY, group).exists()
    code, _, err = batch.run("approve-read", group, "--acknowledge-review", answer="read 1")
    assert code == 1 and "Not at this step" in err and not group_dir(KEY, group).exists()
    _, out, _ = batch.run("review")
    assert "Set aside" in out and f"{state}: SortLab" in out and "exclude GROUP" in out
    assert batch.run("restore", group)[0] == 0
    assert batch.job("SortLab").state == "needs_read_approval" and not group_dir(KEY, group).exists()


def test_several_duplicates_can_be_set_aside_together(batch):
    batch.run("run")
    groups = [batch.ids()["SortLab"], batch.ids()["StatsLab"]]
    assert batch.run("exclude", *groups, "--reason", "duplicates of other work")[0] == 0
    assert {batch.job("SortLab").state, batch.job("StatsLab").state} == {"excluded"}


def test_a_project_prepared_earlier_can_be_set_aside_and_its_disclosure_cannot_be_sent(batch):
    groups = _ready_to_send(batch, "SortLab")
    assert batch.run("defer", *groups)[0] == 0
    code, _, err = batch.run("send", *groups, answer="send 1 to anthropic")
    assert code == 1 and "Not at this step" in err and batch.log == []
    assert batch.run("restore", *groups)[0] == 0
    assert batch.job("SortLab").state == "needs_disclosure_approval"
    assert batch.log == []


def test_setting_aside_a_published_project_does_not_hide_that_it_was_published(batch):
    groups = _staged(batch, "SortLab")
    name = bw.latest_stage(KEY, groups[0]).repo_name
    assert batch.run("publish", *groups, answer="publish 1 private")[0] == 0
    batch.run("exclude", *groups, "--reason", "done")
    assert batch.run("resume")[0] == 0
    assert batch.job("SortLab").state == "published"


# --- validation -----------------------------------------------------------------------------------------------


def test_plans_are_prepared_and_nothing_is_run(batch):
    _proposed(batch, "ShelfTracker")
    job = batch.job("ShelfTracker")
    assert set(job.checkpoints["plans"]) == {"compile", "run"}
    assert batch.runner.calls == []
    _, out, _ = batch.run("review")
    assert "compile not run yet" in out and "test unavailable" in out and "Plans ready: compile, run" in out


def test_a_project_without_an_inspector_reports_unavailable_and_still_moves_on(batch):
    groups = _proposed(batch, "SortLab")
    job = batch.job("SortLab")
    assert job.checkpoints["plan_notes"]["all"].startswith("validation unavailable")
    assert job.state == "needs_name"
    _, out, _ = batch.run("review")
    assert "validation unavailable" in out
    code, _, err = batch.run("validate", groups[0], "--action", "compile", answer="compile 1")
    assert code == 1 and "Manual review needed" in err


def test_validation_needs_its_own_phrase_and_shows_the_risk(batch):
    groups = _proposed(batch, "ShelfTracker")
    for answer in ("yes", "compile 1", "run my code", "compile"):
        code, out, _ = batch.run("validate", *groups, "--action", "run", answer=answer)
        assert code == 1 and "Nothing was run" in out
    assert batch.runner.calls == []
    assert "NOT A SANDBOX" in out
    code, out, _ = batch.run("validate", *groups, "--action", "compile", answer="compile 1")
    assert code == 0 and "compile_passed" in out
    assert [Path_name(c.argv[0]) for c in batch.runner.project_calls] == ["javac"]
    assert batch.run("validate", *groups, "--action", "run", answer="run my code 1")[0] == 0
    assert {r.action for r in list_validation_results(KEY, groups[0])} == {"compile", "run"}


def Path_name(value):
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def test_program_output_stays_out_of_the_batch_output(batch):
    groups = _proposed(batch, "ShelfTracker")
    batch.runner.stdout = b"sample person printed by the program\n"
    code, out, _ = batch.run("validate", *groups, "--action", "run", answer="run my code 1")
    assert code == 0 and "sample person" not in out


def test_results_reach_the_stage_automatically_with_acceptances_intact(batch):
    groups = _staged(batch, "ShelfTracker")
    before = bw.latest_stage(KEY, groups[0])
    assert batch.run("validate", *groups, "--action", "compile", answer="compile 1")[0] == 0
    after = bw.latest_stage(KEY, groups[0])
    assert after.id != before.id and {f.path for f in after.files} == {f.path for f in before.files}
    root = pw.stage_dir(KEY, groups[0], after.id)
    assert "`compile_passed`" in (root / "PROVENANCE.md").read_text(encoding="utf-8")
    assert "compiled (exit code 0)" in (root / "README.md").read_text(encoding="utf-8")


# --- names, staging and findings --------------------------------------------------------------------------------


def test_names_take_the_first_valid_suggestion_or_yours(batch):
    groups = _proposed(batch, "SortLab", "StatsLab")
    custom = f"{groups[1]}=custom-sorted-items"
    code, out, _ = batch.run("accept-names", groups[0], custom, answer="accept 2")
    assert code == 0 and "invented-" in out and "custom-sorted-items" in out
    assert bw.current_proposal(KEY, groups[1]).accepted_name == "custom-sorted-items"
    assert batch.job("SortLab").state == "needs_staging"


def test_every_valid_name_is_shown_with_a_visible_default(batch):
    groups = _proposed(batch, "SortLab")
    proposal = bw.current_proposal(KEY, groups[0])
    valid = [n.slug for n in proposal.names if n.valid]
    assert len(valid) == 2
    _, out, _ = batch.run("review")
    line = next(l for l in out.splitlines() if "Name choices:" in l)
    assert f"{valid[0]} (default)" in line and valid[1] in line and "GROUP=NAME" in line
    assert "Repository:  " + valid[0] + " (proposed)" in out
    code, out, _ = batch.run("accept-names", *groups, answer="no")
    assert code == 1 and valid[0] in out and f"other choices: {valid[1]}" in out
    assert bw.current_proposal(KEY, groups[0]).status != "accepted"  # the default is not a decision
    assert batch.run("accept-names", f"{groups[0]}={valid[1]}", answer="accept 1")[0] == 0
    assert bw.current_proposal(KEY, groups[0]).accepted_name == valid[1]
    _, out, _ = batch.run("review")
    assert "Name choices:" not in out and f"{valid[1]} (chosen)" in out


def test_name_choices_are_hidden_by_redact(batch):
    _proposed(batch, "SortLab")
    _, out, _ = batch.run("--redact", "review")
    assert "invented-" not in out and "Name choices: [hidden]" in out


def test_two_projects_cannot_take_one_name(batch):
    groups = _proposed(batch, "SortLab", "StatsLab")
    code, _, err = batch.run("accept-names", f"{groups[0]}=same-repo-name", f"{groups[1]}=same-repo-name", answer="accept 2")
    assert code == 1 and "same repository name" in err


def test_staging_uses_only_approved_files_and_is_always_private(batch):
    groups = _named(batch, "ShelfTracker")
    code, out, _ = batch.run("stage", *groups, *DECLARED, answer="no")
    assert code == 1 and "Not staged" in out and bw.latest_stage(KEY, groups[0]) is None
    assert "data/sample.csv" not in out and ".env" not in out and "config.properties" not in out
    assert batch.run("stage", *groups, *DECLARED, answer="stage 1")[0] == 0
    stage = bw.latest_stage(KEY, groups[0])
    assert stage.visibility == "private"
    assert not any(f.path.startswith("data/") for f in stage.files)
    with pytest.raises(SystemExit):
        bcli.main(["stage", *groups, *DECLARED, "--visibility", "public"])


def test_a_project_readme_is_left_out_rather_than_failing_the_stage(batch):
    groups = _named(batch, "SortLab")
    assert batch.run("stage", *groups, *DECLARED, answer="stage 1")[0] == 0
    stage = bw.latest_stage(KEY, groups[0])
    assert {f.path for f in stage.files} == {"sorter.py", "requirements.txt", "README.md", "PROVENANCE.md"}
    assert next(f for f in stage.files if f.path == "README.md").generated


def test_a_credential_holds_only_its_own_project(batch):
    groups = _staged(batch, "LeakyTool", "SortLab")
    assert batch.job("LeakyTool").state == "needs_findings_review"
    assert batch.job("SortLab").state == "needs_publish_approval"
    _, out, _ = batch.run("review")
    assert "publishing review" in out and FAKE_PASSWORD not in out
    code, _, err = batch.run("publish", *groups, answers=["publish x"])
    assert code == 1 and "Not at this step" in err and batch.github.created == []


def test_accepting_findings_in_the_existing_command_moves_the_project_on(batch):
    groups = _staged(batch, "LeakyTool")
    stage = bw.latest_stage(KEY, groups[0])
    finding = next(f for f in pw.run_checks(stage) if f.kind == "hardcoded_credential")
    for f in pw.run_checks(stage):
        if f.blocks:
            stage = pw.resolve_finding(stage, [f], f.id, "invented sample")
    assert finding.resolvable
    assert batch.run("resume")[0] == 0
    assert batch.job("LeakyTool").state == "needs_publish_approval"


# --- publishing ------------------------------------------------------------------------------------------------


def test_one_approval_publishes_the_listed_scope_privately(batch):
    groups = _staged(batch, "SortLab", "StatsLab")
    names = {bw.latest_stage(KEY, g).repo_name for g in groups}
    code, out, _ = batch.run("publish", *groups, answer="publish 2 private")  # one answer, for both
    assert code == 0 and "Scope of this approval: 2 new PRIVATE repositories" in out and "Published 2 of 2" in out
    assert all(name in out for name in names) and "separate decision" in out
    assert {c.name.split("/")[1] for c in batch.github.created} == names
    assert all("--private" in c.argv and "--public" not in c.argv for c in batch.github.created)
    states = bw.load_queue(KEY).jobs
    assert all(states[g].state == "published" for g in groups)
    for group in groups:  # every stage keeps its own recorded approval and audit trail
        assert len(list(group_dir(KEY, group).glob("publish-approval-*.json"))) == 1
        audit = (group_dir(KEY, group) / "audit.jsonl").read_text(encoding="utf-8")
        assert '"outcome": "publish_approved"' in audit and '"outcome": "published"' in audit


def test_the_scope_is_shown_and_nothing_is_published_without_the_exact_phrase(batch):
    groups = _staged(batch, "SortLab", "StatsLab")
    for answer in ("yes", "approve", "publish 1 private", "publish 2", "publish 2 public", ""):
        code, out, _ = batch.run("publish", *groups, answer=answer)
        assert code == 1 and "Nothing was published" in out and "Scope of this approval" in out
    assert batch.github.created == [] and not list(group_dir(KEY, groups[0]).glob("publish-approval-*"))


def test_publishing_needs_a_person(batch):
    groups = _staged(batch, "SortLab")

    def nobody(prompt):
        raise EOFError

    assert bcli.main(["publish", *groups], input_fn=nobody) == 1
    assert batch.github.created == [] and batch.job("SortLab").state == "needs_publish_approval"


def test_a_failure_defers_only_that_project(batch, monkeypatch):
    groups = _staged(batch, "SortLab", "StatsLab")
    real = pcli._publish

    def fails_first(args, key, group, stage, terms, inspector):
        if group.id == groups[0]:
            raise pw.PublishRefused("the push failed")
        return real(args, key, group, stage, terms, inspector)

    monkeypatch.setattr(pcli, "_publish", fails_first)
    code, out, err = batch.run("publish", *groups, answer="publish 2 private")
    assert code == 1 and "Published 1 of 2" in out and "Failed: 1" in out and "the push failed" in err
    assert len(batch.github.created) == 1


def test_a_finding_that_appears_after_staging_defers_that_project_and_the_rest_continue(batch):
    groups = _staged(batch, "SortLab", "StatsLab")
    root = pw.stage_dir(KEY, groups[0], bw.latest_stage(KEY, groups[0]).id)
    real = pw.run_checks
    first = bw.latest_stage(KEY, groups[0]).id

    def flagged(stage, **kwargs):
        found = real(stage, **kwargs)
        return [*found, pw.Finding("x", "privacy", "new", "high", None, None, "new")] if stage.id == first else found

    pw.run_checks, restore = flagged, real
    try:
        code, out, _ = batch.run("publish", *groups, answer="publish 1 private")
    finally:
        pw.run_checks = restore
    assert code == 0 and f"deferred: {groups[0]}" in out and len(batch.github.created) == 1
    assert root.is_dir()


def test_the_published_readme_is_about_the_project(batch):
    groups = _staged(batch, "ShelfTracker", "SortLab")
    for group in groups:
        stage = bw.latest_stage(KEY, group)
        readme = (pw.stage_dir(KEY, group, stage.id) / "README.md").read_text(encoding="utf-8")
        footnote = "_Documentation prepared with assistance from [PortfolioForge](https://github.com/lindseyrich2/PortfolioForge)._"
        assert readme.rstrip().endswith(footnote) and "PortfolioForge" not in readme.replace(footnote, "")
        assert "Draft generated" not in readme and "have **not** been run or tested" in readme


def test_attribution_is_in_the_readme_and_does_not_cause_endless_refreshes(batch):
    groups = _named(batch, "SortLab")
    text = "Includes code credited to an invented vendor."
    assert batch.run("stage", *groups, "--ownership", "includes-third-party", "--license", "none", "--attribution", text, answer="stage 1")[0] == 0
    stage = bw.latest_stage(KEY, groups[0])
    readme = (pw.stage_dir(KEY, groups[0], stage.id) / "README.md").read_text(encoding="utf-8")
    assert "## Attribution" in readme and text in readme
    assert batch.run("resume")[0] == 0
    assert bw.latest_stage(KEY, groups[0]).id == stage.id


def test_a_taken_name_is_never_overwritten_and_others_still_publish(batch):
    groups = _staged(batch, "SortLab", "StatsLab")
    names = {g: bw.latest_stage(KEY, g).repo_name for g in groups}
    batch.github.existing.add(names[groups[0]])
    code, out, _ = batch.run("publish", *groups, answer="publish 1 private")
    assert code == 0 and "already exists, and is never overwritten" in out
    assert [c.name.split("/")[1] for c in batch.github.created] == [names[groups[1]]]


def test_a_public_stage_is_never_published_by_a_batch(batch):
    groups = _staged(batch, "SortLab")
    stage = bw.latest_stage(KEY, groups[0])
    from dataclasses import replace
    public = replace(stage, visibility="public")
    pw.save_stage(public)
    assert batch.run("resume")[0] == 0
    assert batch.job("SortLab").state == "needs_staging"  # an edited stage is not trusted
    code, _, err = batch.run("publish", *groups, answer="publish x")
    assert code == 1 and "Not at this step" in err and batch.github.created == []


def test_a_real_public_stage_is_refused_by_a_batch(batch):
    groups = _named(batch, "SortLab")
    from portfolioforge.understanding.eligibility import assess_run
    ctx = bw.load_context()
    group = ctx.run.group(groups[0])
    chosen = bcli._selected_for_publication(ctx, groups[0])
    pw.create_stage(inventory_key=KEY, group=group, eligibility={e.group_id: e for e in assess_run(ctx.run, ctx.inventory)}[group.id],
                    proposal=bw.current_proposal(KEY, groups[0]), inventory=ctx.inventory, privacy=ctx.privacy,
                    file_ids=[c.file_id for c in chosen], visibility="public", ownership="mine", license_id="none")
    assert batch.run("resume")[0] == 0
    assert batch.job("SortLab").state == "needs_publish_approval"
    code, out, _ = batch.run("publish", *groups, answer="publish 1 private")
    assert "A batch only publishes private repositories" in out and batch.github.created == []


def test_published_projects_are_recognized_and_left_alone(batch):
    groups = _staged(batch, "SortLab")
    name = bw.latest_stage(KEY, groups[0]).repo_name
    assert batch.run("publish", *groups, answer="publish 1 private")[0] == 0
    records = sorted(p.name for p in group_dir(KEY, groups[0]).iterdir())
    for _ in range(2):
        assert batch.run("run", "--provider", "anthropic")[0] == 0
    assert batch.job("SortLab").state == "published"
    assert sorted(p.name for p in group_dir(KEY, groups[0]).iterdir()) == records  # no new stage, plan or proposal
    code, _, err = batch.run("publish", *groups, answer="publish 1 private")
    assert code == 1 and "Not at this step" in err and len(batch.github.created) == 1


def test_a_project_handled_before_the_batch_existed_is_recognized(batch):
    batch.run("run")
    group = batch.ids()["SortLab"]
    from portfolioforge.understanding.store import append_audit
    append_audit(KEY, group, {"stage_id": "st_old", "visibility": "private", "outcome": "published"})
    assert batch.run("resume")[0] == 0
    assert batch.job("SortLab").state == "published"


def _data_files():
    return tree_hash(paths.data_dir())


def test_repeating_run_and_resume_changes_no_existing_record(batch):
    groups = _staged(batch, "SortLab", "StatsLab")
    name = bw.latest_stage(KEY, groups[0]).repo_name
    assert batch.run("publish", groups[0], answer="publish 1 private")[0] == 0
    before = _data_files()
    for command in (("run", "--provider", "anthropic"), ("resume",), ("status",), ("review",)):
        assert batch.run(*command)[0] == 0
    after = _data_files()
    changed = {p for p in before if before[p] != after.get(p)}
    assert changed <= {f"batch/{KEY}/queue.json"}, changed
    assert set(before) <= set(after)  # nothing removed
    assert set(after) - set(before) == set()  # and nothing new to review


def test_new_results_add_a_stage_and_leave_the_old_one_in_place(batch):
    groups = _staged(batch, "SortLab")
    old = bw.latest_stage(KEY, groups[0])
    old_files = tree_hash(pw.stage_dir(KEY, groups[0], old.id))
    source = next(p for p in group_dir(KEY, groups[0]).glob("publish-stage-*"))
    old_record = source.read_bytes()
    proposal = bw.current_proposal(KEY, groups[0])
    from dataclasses import replace
    from portfolioforge.understanding.store import save_proposal
    save_proposal(replace(proposal, user_summary="A different summary, written by the owner."))
    assert batch.run("resume")[0] == 0
    assert bw.latest_stage(KEY, groups[0]).id != old.id
    assert tree_hash(pw.stage_dir(KEY, groups[0], old.id)) == old_files and source.read_bytes() == old_record


def test_the_only_repository_operation_is_creating_a_new_one(batch):
    source = "".join(open(m.__file__, encoding="utf-8").read() for m in (bw, bcli, pw, pcli))
    assert not re.search(r"--force|\bgit\b.*\bpush\b|repo.*(?:delete|edit|rename|archive)|gh api -X|\"-X\"", source)
    groups = _staged(batch, "SortLab")
    name = bw.latest_stage(KEY, groups[0]).repo_name
    batch.github.existing.add(name)  # the fake fails on any GitHub call it doesn't expect
    assert batch.run("publish", *groups, answer="publish 1 private")[0] == 0
    assert batch.github.created == []


def test_originals_are_never_changed(batch):
    before = tree_hash(batch.root)
    groups = _staged(batch, "ShelfTracker", "SortLab")
    batch.run("validate", groups[0], "--action", "compile", answer="compile 1")
    assert tree_hash(batch.root) == before


# --- the review -------------------------------------------------------------------------------------------------


def test_review_brings_everything_together(batch):
    _staged(batch, "SortLab", "ShelfTracker")
    code, out, _ = batch.run("review")
    assert code == 0
    for text in ("SortLab", "Repository:", "invented-", "(chosen)", "Readiness:", "Validation:", "Privacy:", "Decisions needed:"):
        assert text in out, text
    assert FAKE_PASSWORD not in out


def test_review_lists_each_decision_with_the_projects_it_covers(batch):
    batch.run("run")
    _, out, _ = batch.run("review")
    assert "allow reading this project's files on this computer" in out and "approve-read" in out
    assert batch.ids()["SortLab"] in out.split("Decisions needed:")[1]
    assert "Personal" not in out  # set aside, not asking for anything


def test_review_redacted_shows_no_names(batch):
    _proposed(batch, "SortLab")
    for command in ("review", "status"):
        _, out, _ = batch.run("--redact", command)
        for name in ("SortLab", "invented-", "sorter.py", str(batch.root)):
            assert name not in out, name


def test_duplicate_groups_are_shown_as_concerns(tmp_path, monkeypatch, fake_system, capsys):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "pf-data"))
    source = "def sort_items(items):\n    return sorted(items)\n"
    root = make_tree(tmp_path / "Synthetic", {
        "Work/SortLab/sorter.py": source, "Work/SortLab/helper.py": "x = 1\n", "Work/SortLab/README.md": "# S\n",
        "Work/SortLab - Copy/sorter.py": source, "Work/SortLab - Copy/helper.py": "x = 1\n",
        "Work/SortLab - Copy/README.md": "# S\n",
    })
    with JsonlSink(paths.inventories_dir() / KEY) as sink:
        run_scan(plan_targeted([root], system=fake_system), sink=sink)
    assert gcli.main(["propose"]) == 0
    capsys.readouterr()
    assert bcli.main(["run"]) == 0
    capsys.readouterr()
    assert bcli.main(["review"]) == 0
    assert "Duplicate:" in capsys.readouterr().out


def test_help_explains_the_workflow(capsys):
    with pytest.raises(SystemExit) as stopped:
        bcli.main(["--help"])
    assert stopped.value.code == 0
    out = capsys.readouterr().out
    for word in ("run", "resume", "status", "review", "approve-read", "send", "validate", "stage", "publish", "PRIVATE"):
        assert word.casefold() in out.casefold()


def test_there_is_no_way_to_ask_for_public_or_skip_confirmation():
    parser = bcli.build_parser()
    text = parser.format_help()
    assert "--yes" not in text and "--public" not in text
    for argv in (["send", "g", "--yes"], ["publish", "g", "--yes"], ["validate", "g", "--action", "run", "--yes"],
                 ["approve-read", "g", "--yes"], ["publish", "g", "--visibility", "public"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_batch_code_names_no_language():
    for module in (bw, bcli):
        source = open(module.__file__, encoding="utf-8").read()
        assert not re.findall(r"(?i)\b(java|javac|maven|gradle|ant|netbeans|jupyter)\b", source)
