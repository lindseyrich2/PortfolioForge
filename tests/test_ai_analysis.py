"""AI understanding and naming: Anthropic adapter, approval enforcement, local verification.

Every provider response here is a synthetic, mocked answer. No test makes a
network request (sockets are blocked in the ones that exercise a provider).
"""

from __future__ import annotations

import json
import re
import socket
import sys
from types import SimpleNamespace

import pytest

from portfolioforge import paths
from portfolioforge.understanding import approve_manifest, build_manifest, submit
from portfolioforge.understanding.analysis import (
    InvalidModelOutput,
    ProposalError,
    accept_proposal,
    build_proposal,
    edit_proposal,
    slug_problems,
)
from portfolioforge.understanding.anthropic_provider import AnthropicProvider, ProviderRequestFailed
from portfolioforge.understanding.disclosure import DisclosureNotApproved, ProviderNotConfigured, get_provider
from test_understanding import _accept, _approve, _pack, world  # noqa: F401  (world is a fixture)


class FakeClient:
    """Stands in for anthropic.Anthropic(); records exactly what it was asked to send."""

    def __init__(self, text: str | None = None, stop_reason: str = "end_turn", error: Exception | None = None):
        self.calls: list[dict] = []
        self._text, self._stop, self._error = text, stop_reason, error
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        content = [SimpleNamespace(type="text", text=self._text)] if self._text is not None else []
        return SimpleNamespace(content=content, stop_reason=self._stop, _request_id="req_synthetic")


@pytest.fixture
def no_network(monkeypatch):
    def blocked(*a, **k):
        raise AssertionError("tests must not open network connections")
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def _ids(pack, predicate):
    return [i.id for i in pack.items if predicate(i)]


def _claim(text, ids):
    return {"text": text, "evidence_ids": list(ids)}


def _step(text, ids, command="", kind="from_evidence"):
    return {"text": text, "command": command, "kind": kind, "evidence_ids": list(ids)}


def good_answer(pack, **overrides) -> dict:
    train_def = _ids(pack, lambda i: i.text.startswith("def train"))
    sklearn = _ids(pack, lambda i: "sklearn" in i.text)
    numpy = _ids(pack, lambda i: i.text == "import numpy as np")
    readme = _ids(pack, lambda i: i.kind == "readme")
    sql = _ids(pack, lambda i: i.project_path == "queries.sql")
    answer = {
        "purpose": _claim("Trains a naive Bayes classifier that labels messages as spam.", train_def + readme),
        "functionality": [_claim("Defines a training function for a naive Bayes spam classifier.", train_def),
                          _claim("Stores messages and labels in SQL tables joined by id.", sql)],
        "languages": [_claim("Python", train_def), _claim("SQL", sql)],
        "technologies": [_claim("scikit-learn", sklearn), _claim("NumPy", numpy)],
        "engineering_concepts": [_claim("Supervised text classification with naive Bayes.", train_def)],
        "uncertainties": ["How messages are vectorized is not shown."],
        "attribution_concerns": [],
        "insufficient_evidence": False,
        "repository_names": [
            {"slug": "naive-bayes-spam-filter", "rationale": "describes the classifier", "evidence_ids": train_def},
            {"slug": "sms-spam-classifier", "rationale": "describes the task", "evidence_ids": readme},
        ],
        "title": "Naive Bayes Spam Filter",
        "summary": _claim("A naive Bayes spam classifier built with scikit-learn.", train_def + sklearn),
        "readme_sections": [{"heading": "Overview", "items": [
            _claim("Classifies messages as spam or not spam.", readme)]}],
        "usage_guide": {
            "prerequisites": [_step("Python 3 with pip.", train_def)],
            "dependencies": [_step("numpy", numpy), _step("scikit-learn", sklearn)],
            "setup_steps": [_step("Install the listed dependencies.", numpy + sklearn,
                                  command="pip install -r requirements.txt")],
            "run_commands": [_step("Run the training script.", train_def, command="python train.py")],
            "test_instructions": [_step("Call train() on a few labelled messages and check that a model is "
                                        "returned.", train_def, kind="manual")],
            "data_and_configuration": [_step("A CSV of messages with text and label columns.", readme,
                                             kind="inferred")],
            "compatibility_notes": [],
            "missing_information": [],
            "needs_local_inspection": False,
        },
    }
    answer.update(overrides)
    return answer


def wire(answer: dict) -> dict:
    """Convert a readable nested test answer into the flat structured-output format the API returns."""
    claims = [{"category": "purpose", "heading": "", **answer["purpose"]},
              {"category": "summary", "heading": "", **answer["summary"]}]
    for key, category in (("functionality", "functionality"), ("languages", "language"),
                          ("technologies", "technology"), ("engineering_concepts", "engineering_concept")):
        claims += [{"category": category, "heading": "", **c} for c in answer[key]]
    for section in answer["readme_sections"]:
        claims += [{"category": "readme", "heading": section["heading"], **c} for c in section["items"]]
    usage = answer["usage_guide"]
    steps = []
    for field, section in (("prerequisites", "prerequisite"), ("dependencies", "dependency"),
                           ("setup_steps", "setup"), ("run_commands", "run"), ("test_instructions", "test"),
                           ("data_and_configuration", "data_and_configuration")):
        steps += [{"section": section, **s} for s in usage[field]]
    return {"title": answer["title"], "claims": claims, "repository_names": answer["repository_names"],
            "steps": steps, "compatibility_notes": usage["compatibility_notes"],
            "uncertainties": answer["uncertainties"], "attribution_concerns": answer["attribution_concerns"],
            "missing_information": usage["missing_information"],
            "insufficient_evidence": answer["insufficient_evidence"],
            "needs_local_inspection": usage["needs_local_inspection"]}


def _anthropic_manifest(world, model="claude-opus-5-5"):
    pack, approval, _ = _pack(world)
    manifest, payload = build_manifest(pack, "anthropic", model)
    return pack, manifest, payload


# --- the approved payload is the complete request ---------------------------------------


def test_anthropic_payload_is_the_complete_request_body(world):
    pack, manifest, payload = _anthropic_manifest(world)
    body = json.loads(payload)
    assert set(body) == {"model", "max_tokens", "system", "output_config", "messages"}
    assert body["model"] == manifest.model == "claude-opus-5-5"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert len(body["messages"]) == 1 and body["messages"][0]["role"] == "user"
    dry, dry_payload = build_manifest(pack, "dry-run", "dry-run")
    assert json.loads(dry_payload)["evidence"] == json.loads(body["messages"][0]["content"].split("\n", 1)[1])["evidence"]
    assert str(world["root"]) not in payload.decode() and "SpamFilter" not in payload.decode()
    assert manifest.request["external"] is True and manifest.request["format"] == "anthropic_messages_request"
    assert "fallbacks" not in body and "betas" not in body  # no other model can receive the content


def test_submit_sends_exactly_the_approved_fields(world, no_network):
    pack, manifest, payload = _anthropic_manifest(world)
    client = FakeClient(json.dumps(wire(good_answer(pack))))
    result = submit(manifest, payload, approve_manifest(manifest), AnthropicProvider(manifest.model, client))
    assert client.calls == [json.loads(payload)]
    assert result.sent and result.status.startswith("completed") and result.response


def test_default_model_is_sonnet_and_opus_is_explicit(world):
    pack, _, _ = _pack(world)
    default, payload = build_manifest(pack, "anthropic", get_provider("anthropic").model)
    assert default.model == json.loads(payload)["model"] == "claude-sonnet-5-5"
    assert json.loads(payload)["output_config"]["effort"] == "medium"
    opus, opus_payload = build_manifest(pack, "anthropic", "claude-opus-5-5")
    assert json.loads(opus_payload)["model"] == "claude-opus-5-5"


def test_cost_estimate_is_a_labelled_upper_bound(world):
    from portfolioforge.understanding.analysis import ESTIMATE_BYTES_PER_TOKEN, STRUCTURED_OUTPUT_OVERHEAD_TOKENS
    from portfolioforge.understanding.disclosure import format_manifest
    pack, _, _ = _pack(world)
    manifest, payload = build_manifest(pack, "anthropic", "claude-sonnet-5-5", max_tokens=8000)
    cost = manifest.request["cost"]
    expected_input = -(-len(payload) // ESTIMATE_BYTES_PER_TOKEN) + STRUCTURED_OUTPUT_OVERHEAD_TOKENS
    assert cost["input_tokens_estimate"] == expected_input and cost["max_output_tokens"] == 8000
    assert cost["max_cost_usd"] == round(expected_input / 1e6 * 2.0 + 8000 / 1e6 * 10.0, 4)
    shown = format_manifest(manifest)
    assert "at most about $" in shown and "not an exact charge" in shown and "claude-sonnet-5-5" in shown
    unknown, _ = build_manifest(pack, "anthropic", "claude-some-future-model")
    assert "price unknown" in format_manifest(unknown)


def test_manifest_shows_every_request_component(world):
    from portfolioforge.understanding.disclosure import format_manifest
    _, manifest, payload = _anthropic_manifest(world)
    shown = format_manifest(manifest)
    r = manifest.request
    assert r["fields"] == ["max_tokens", "messages", "model", "output_config", "system"]
    for key in ("system_prompt_sha256", "output_schema_sha256", "messages_sha256"):
        assert r[key][:16] in shown
    assert "changing any part requires a new manifest" in shown


@pytest.mark.parametrize("change", ["model", "effort", "max_tokens", "system_prompt", "schema", "evidence"])
def test_changing_any_request_component_invalidates_the_approval(world, monkeypatch, no_network, change):
    from portfolioforge.understanding import analysis
    pack, _, _ = _pack(world)
    original, original_payload = build_manifest(pack, "anthropic", "claude-sonnet-5-5")
    approval = approve_manifest(original)
    model, kwargs = "claude-sonnet-5-5", {}
    if change == "model":
        model = "claude-opus-5-5"
    elif change == "effort":
        kwargs["effort"] = "high"
    elif change == "max_tokens":
        kwargs["max_tokens"] = 32000
    elif change == "system_prompt":
        monkeypatch.setattr(analysis, "SYSTEM_PROMPT", analysis.SYSTEM_PROMPT + "\nAlso list the author.")
    elif change == "schema":
        monkeypatch.setattr(analysis, "OUTPUT_SCHEMA", {**analysis.OUTPUT_SCHEMA, "description": "changed"})
    else:
        pack = type(pack)(**{**{f: getattr(pack, f) for f in pack.__dataclass_fields__},
                             "items": pack.items[:-1]})
    changed, changed_payload = build_manifest(pack, "anthropic", model, **kwargs)
    assert changed.payload_sha256 != original.payload_sha256
    client = FakeClient("{}")
    with pytest.raises(DisclosureNotApproved):
        submit(changed, changed_payload, approval, AnthropicProvider(model, client))
    with pytest.raises(DisclosureNotApproved):  # old approval + new bytes under the old manifest
        submit(original, changed_payload, approval, AnthropicProvider("claude-sonnet-5-5", client))
    assert client.calls == []


def test_editing_a_saved_manifest_after_approval_is_refused(world, no_network):
    from dataclasses import replace as dc_replace
    _, manifest, payload = _anthropic_manifest(world, model="claude-sonnet-5-5")
    approval = approve_manifest(manifest)
    edited = dc_replace(manifest, model="claude-opus-5-5")  # e.g. someone edits manifest-<id>.json
    client = FakeClient("{}")
    with pytest.raises(DisclosureNotApproved):
        submit(edited, payload, approval, AnthropicProvider("claude-opus-5-5", client))
    assert client.calls == []


# --- approval enforcement --------------------------------------------------------------------


def test_a_dry_run_approval_never_authorizes_an_anthropic_submission(world, no_network):
    pack, _, _ = _pack(world)
    dry_manifest, _ = build_manifest(pack, "dry-run", "dry-run")
    dry_approval = approve_manifest(dry_manifest)
    manifest, payload = build_manifest(pack, "anthropic", "claude-opus-5-5")
    client = FakeClient("{}")
    with pytest.raises(DisclosureNotApproved):
        submit(manifest, payload, dry_approval, AnthropicProvider(manifest.model, client))
    forged = type(dry_approval)(dry_approval.id, manifest.id, dry_approval.payload_sha256, "dry-run", "dry-run",
                                dry_approval.approved_at)
    with pytest.raises(DisclosureNotApproved):
        submit(manifest, payload, forged, AnthropicProvider(manifest.model, client))
    assert client.calls == []


def test_approval_is_bound_to_one_model(world, no_network):
    pack, _, _ = _pack(world)
    opus, opus_payload = build_manifest(pack, "anthropic", "claude-opus-5-5")
    sonnet, sonnet_payload = build_manifest(pack, "anthropic", "claude-sonnet-5-5")
    client = FakeClient("{}")
    with pytest.raises(DisclosureNotApproved):
        submit(sonnet, sonnet_payload, approve_manifest(opus), AnthropicProvider("claude-sonnet-5-5", client))
    with pytest.raises(DisclosureNotApproved):  # right approval, wrong provider instance
        submit(opus, opus_payload, approve_manifest(opus), AnthropicProvider("claude-sonnet-5-5", client))
    assert client.calls == []


@pytest.mark.parametrize("tamper", [
    lambda b: b + b" ",
    lambda b: b.replace(b"spam", b"SPAM", 1),
    lambda b: json.dumps({**json.loads(b), "metadata": {"user_id": "x"}}, sort_keys=True,
                         separators=(",", ":")).encode(),
])
def test_tampered_payloads_are_never_sent(world, no_network, tamper):
    pack, manifest, payload = _anthropic_manifest(world)
    client = FakeClient("{}")
    with pytest.raises(DisclosureNotApproved, match="payload differs"):
        submit(manifest, tamper(payload), approve_manifest(manifest), AnthropicProvider(manifest.model, client))
    assert client.calls == []


def test_unapproved_submission_is_refused(world, no_network):
    pack, manifest, payload = _anthropic_manifest(world)
    client = FakeClient("{}")
    with pytest.raises(DisclosureNotApproved):
        submit(manifest, payload, None, AnthropicProvider(manifest.model, client))
    assert client.calls == []


def test_provider_refuses_a_body_naming_another_model(world, no_network):
    _, manifest, payload = _anthropic_manifest(world)
    with pytest.raises(DisclosureNotApproved):
        AnthropicProvider("claude-sonnet-5-5", FakeClient("{}")).send(payload)


def test_missing_sdk_is_reported_without_contacting_anything(world, monkeypatch, no_network):
    _, manifest, payload = _anthropic_manifest(world)
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import anthropic -> ImportError
    with pytest.raises(ProviderNotConfigured, match="not installed"):
        submit(manifest, payload, approve_manifest(manifest), get_provider("anthropic", manifest.model))


def test_provider_errors_never_echo_request_content(world, no_network):
    _, manifest, payload = _anthropic_manifest(world)
    error = RuntimeError("bad request containing naive Bayes text")
    error.status_code = 400
    with pytest.raises(ProviderRequestFailed) as excinfo:
        submit(manifest, payload, approve_manifest(manifest), AnthropicProvider(manifest.model, FakeClient(error=error)))
    assert "HTTP 400" in str(excinfo.value) and "naive Bayes" not in str(excinfo.value)


@pytest.mark.parametrize(("stop", "status"), [("refusal", "refused"), ("max_tokens", "truncated")])
def test_refusals_and_truncation_produce_no_answer(world, no_network, stop, status):
    _, manifest, payload = _anthropic_manifest(world)
    result = submit(manifest, payload, approve_manifest(manifest),
                    AnthropicProvider(manifest.model, FakeClient("{}", stop_reason=stop)))
    assert result.status == status and result.response is None


# --- local verification of the answer -------------------------------------------------------


def _proposal(world, answer=None, **overrides):
    pack, manifest, payload = _anthropic_manifest(world)
    text = json.dumps(wire(answer if answer is not None else good_answer(pack, **overrides)))
    return build_proposal(response_text=text, sent_body=json.loads(payload), inventory_key="inv",
                          group_id=manifest.group_id, manifest_id=manifest.id, provider="anthropic",
                          model=manifest.model), pack


def test_supported_claims_are_verified_and_ownership_stays_unconfirmed(world):
    proposal, _ = _proposal(world)
    assert proposal.purpose.status == "verified"
    assert all(c.status == "verified" for c in proposal.technologies)
    assert [n.slug for n in proposal.names if n.valid] == ["naive-bayes-spam-filter", "sms-spam-classifier"]
    assert proposal.ownership == "unconfirmed" and proposal.accepted_name is None


def test_unsupported_claims_are_rejected(world):
    pack, _, _ = _anthropic_manifest(world)
    train = _ids(pack, lambda i: i.text.startswith("def train"))
    answer = good_answer(pack,
                         technologies=[_claim("TensorFlow", train), _claim("scikit-learn", ["e_not_sent"])],
                         functionality=[_claim("Achieves 97% accuracy on the test set.", train),
                                        _claim("I built a spam filter for my class.", train),
                                        _claim("Trains a classifier.", [])])
    proposal, _ = _proposal(world, answer)
    reasons = {c.text: c.reason for c in [*proposal.technologies, *proposal.functionality]}
    assert reasons["TensorFlow"] == "technology or language not found in the evidence"
    assert reasons["scikit-learn"] == "cites evidence that was not sent"
    assert "97%" in reasons["Achieves 97% accuracy on the test set."]
    assert "authorship" in reasons["I built a spam filter for my class."]
    assert reasons["Trains a classifier."] == "no evidence cited"
    assert any("rejected by local verification" in f for f in proposal.review_flags)
    readme = proposal.readme_markdown()
    assert "TensorFlow" not in readme and "97%" not in readme and "I built" not in readme


@pytest.mark.parametrize(("slug", "valid"), [
    ("naive-bayes-spam-filter", True),
    ("Loan Account Hierarchy", True),  # normalized to loan-account-hierarchy
    ("hw3-spam-filter", False),
    ("cs101-text-classifier", False),
    ("assignment-2", False),
    ("pythonproject1", False),
    ("final-project", False),
    ("my-spam-filter", False),
    ("filter", False),
    ("spam_filter!!", True),
])
def test_repository_name_rules(slug, valid):
    _, problems = slug_problems(slug)
    assert (not problems) is valid, problems


def test_review_flags_for_thin_or_uncertain_evidence(world):
    proposal, _ = _proposal(world, insufficient_evidence=True,
                            attribution_concerns=["A header comment says 'starter code provided'."],
                            functionality=[],
                            repository_names=[{"slug": "hw1", "rationale": "", "evidence_ids": []}])
    flags = " | ".join(proposal.review_flags)
    for expected in ("insufficient evidence", "third-party or template", "functionality is unclear",
                     "expected 2-3 name suggestions", "no valid descriptive repository name"):
        assert expected in flags


@pytest.mark.parametrize("text", ["not json", json.dumps({"purpose": "x"}), json.dumps([1, 2])])
def test_malformed_answers_are_rejected(world, text):
    pack, manifest, payload = _anthropic_manifest(world)
    with pytest.raises(InvalidModelOutput):
        build_proposal(response_text=text, sent_body=json.loads(payload), inventory_key="inv", group_id="g",
                       manifest_id=manifest.id, provider="anthropic", model=manifest.model)


# --- setup / run / test instructions -----------------------------------------------------------


def test_usage_guide_is_verified_and_labelled(world):
    pack, _, _ = _anthropic_manifest(world)
    train = _ids(pack, lambda i: i.text.startswith("def train"))
    numpy = _ids(pack, lambda i: i.text == "import numpy as np")
    secret = "sk_" + "live_" + "Z9y8X7w6V5u4T3s2"
    guide = good_answer(pack)["usage_guide"] | {
        "dependencies": [_step("numpy", numpy), _step("flask", train)],
        "run_commands": [_step("Run the training script.", train, command="python train.py"),
                         _step("Run the pipeline.", train, command="python run_all.py"),
                         _step("Start the containers.", train, command="docker compose up")],
        "test_instructions": [_step("All tests pass when you run pytest.", train, command="pytest"),
                              _step("Call train() on two messages; it returns a fitted model.", train,
                                    kind="manual")],
        "data_and_configuration": [_step(f"Set API_TOKEN={secret} before running.", train)],
        "compatibility_notes": [{"text": "Written for an older scikit-learn.", "original_requirement":
                                 "scikit-learn as imported in train.py", "modern_alternative":
                                 "a current scikit-learn release", "evidence_ids": train}],
        "missing_information": ["The dataset file is not in the evidence."],
        "needs_local_inspection": True,
    }
    proposal, _ = _proposal(world, usage_guide=guide)
    u = proposal.usage
    assert [(d.text, d.status) for d in u.dependencies] == [("numpy", "verified"), ("flask", "rejected")]
    assert [r.status for r in u.run_commands] == ["verified", "unverified", "unverified"]
    assert u.test_instructions[0].status == "rejected" and "has not run" in u.test_instructions[0].reason
    assert u.test_instructions[1].status == "verified" and u.test_instructions[1].kind == "manual"
    assert secret not in json.dumps([i.text for _, i in u.instructions()])
    flags = " | ".join(proposal.review_flags)
    assert "additional approved local inspection" in flags and "unverified" in flags

    readme = proposal.readme_markdown()
    for heading in ("## Requirements", "## Setup", "## Running", "## Testing", "## Data and configuration",
                    "## Compatibility notes", "## Missing information"):
        assert heading in readme
    assert "python train.py" in readme and "✓ Run the training script." in readme
    assert "⚠ Unverified suggestion: Run the pipeline." in readme and "**Legend:**" in readme
    assert "To run it as it was written, use the original requirements" in readme
    assert "flask" not in readme and "All tests pass" not in readme and secret not in readme
    assert "Suggested modern alternative (not applied to the original code)" in readme
    assert "have **not** been run or tested" in readme and "(manual check: expected behavior described" in readme


def test_running_and_testing_are_marked_undetermined_when_missing(world):
    pack, _, _ = _anthropic_manifest(world)
    guide = {k: [] for k in ("prerequisites", "dependencies", "setup_steps", "run_commands", "test_instructions",
                             "data_and_configuration", "compatibility_notes", "missing_information")}
    proposal, _ = _proposal(world, usage_guide={**guide, "needs_local_inspection": True})
    readme = proposal.readme_markdown()
    assert readme.count("Not determined from the available evidence") == 2
    assert "approved local inspection" in readme
    assert {"no verified run instructions", "no test instructions"} <= set(proposal.review_flags)


# --- review: edit and accept ----------------------------------------------------------------------


def test_edit_and_accept_never_publish_or_claim_ownership(world):
    proposal, _ = _proposal(world)
    with pytest.raises(ProposalError):
        accept_proposal(proposal, "hw3-spam", acknowledge_flags=True, taken_names=set())
    with pytest.raises(ProposalError, match="name problems"):
        edit_proposal(proposal, add_name="cs101-project")
    edited = edit_proposal(proposal, title="Spam Filter", add_name="message-spam-detector",
                           summary="My own one-line description.")
    assert edited.status == "edited" and edited.user_title == "Spam Filter"
    assert "My own one-line description." in edited.readme_markdown()
    if edited.review_flags:
        with pytest.raises(ProposalError, match="review flags"):
            accept_proposal(edited, "message-spam-detector", acknowledge_flags=False, taken_names=set())
    with pytest.raises(ProposalError, match="already uses"):
        accept_proposal(edited, "naive-bayes-spam-filter", acknowledge_flags=True,
                        taken_names={"naive-bayes-spam-filter"})
    accepted = accept_proposal(edited, "message-spam-detector", acknowledge_flags=True, taken_names=set())
    assert accepted.status == "accepted" and accepted.accepted_name == "message-spam-detector"
    assert accepted.ownership == "unconfirmed"


# --- command line, with a fake Anthropic client ------------------------------------------------------


def test_cli_end_to_end_with_mocked_anthropic(tmp_path, monkeypatch, fake_system, capsys, no_network):
    from conftest import make_tree
    from portfolioforge.grouping import cli as gcli
    from portfolioforge.grouping.store import load_run
    from portfolioforge.inventory import JsonlSink, plan_targeted, run_scan
    from portfolioforge.understanding import cli as ucli
    from test_understanding import NOTEBOOK, _python_source

    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "pf-data"))
    root = make_tree(tmp_path / "Synthetic", {
        "Projects/SpamFilter/train.py": _python_source(tmp_path / "EXECUTED.txt"),
        "Projects/SpamFilter/explore.ipynb": NOTEBOOK,
        "Projects/SpamFilter/README.md": "# Spam Filter\n\nClassifies messages as spam or not spam.\n",
        "Projects/SpamFilter/requirements.txt": "numpy\nscikit-learn\n",
    })
    with JsonlSink(paths.inventories_dir() / "20260401T000000Z") as sink:
        run_scan(plan_targeted([root], system=fake_system), sink=sink)
    gcli.main(["propose"])
    group = next(g.id for g in load_run("20260401T000000Z").groups if g.working_title.text == "SpamFilter")
    gcli.main(["accept", group])
    for args in (["approve-files", group, "--yes"], ["extract", group]):
        assert ucli.main(args) == 0
    capsys.readouterr()

    assert ucli.main(["manifest", group, "--provider", "anthropic"]) == 0
    manifest_id = re.search(r"Disclosure manifest (m_\w+)", capsys.readouterr().out).group(1)

    from portfolioforge.understanding.store import latest_pack
    pack = latest_pack("20260401T000000Z", group)
    client = FakeClient(json.dumps(wire(good_answer(pack))))
    monkeypatch.setattr(ucli, "make_provider", lambda name, model: AnthropicProvider(model, client))

    assert ucli.main(["submit", group, manifest_id, "--yes"]) == 1  # not approved yet
    assert "not been approved" in capsys.readouterr().err
    assert ucli.main(["approve-manifest", group, manifest_id, "--yes"]) == 0
    assert ucli.main(["submit", group, manifest_id], input_fn=lambda prompt: "no") == 1  # final confirmation
    assert client.calls == []
    capsys.readouterr()

    assert ucli.main(["submit", group, manifest_id], input_fn=lambda prompt: "send") == 0
    out = capsys.readouterr().out
    assert len(client.calls) == 1 and "claims verified" in out
    proposal_id = re.search(r"Proposal (pr_\w+)", out).group(1)

    assert ucli.main(["--redact", "show-proposal", group, proposal_id]) == 0
    redacted = capsys.readouterr().out
    assert "naive-bayes" not in redacted and "SpamFilter" not in redacted and "Claims:" in redacted
    assert ucli.main(["show-proposal", group, proposal_id, "--claims"]) == 0
    assert "evidence e_" in capsys.readouterr().out
    assert ucli.main(["readme", group, proposal_id]) == 0
    assert ucli.main(["accept-proposal", group, proposal_id, "--name", "naive-bayes-spam-filter",
                      "--acknowledge-flags"]) == 0
    out = capsys.readouterr().out
    assert "Nothing was created or published" in out

    folder = paths.data_dir() / "understanding" / "20260401T000000Z" / group
    audit = (folder / "audit.jsonl").read_text()
    assert '"outcome": "refused"' in audit and "completed" in audit
    assert "naive Bayes" not in audit and "train.py" not in audit
    assert list(folder.glob("README-draft-*.md")) and list(folder.glob("proposal-*.json"))
    assert not (tmp_path / "EXECUTED.txt").exists()
