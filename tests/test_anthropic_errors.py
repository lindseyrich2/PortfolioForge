"""Regression tests for the HTTP 400 seen in the live synthetic test.

1. The output schema must stay within the structured-output feature set and stay
   flat enough to compile (the API rejects overly complex schemas with a 400).
2. API errors must be reported with a useful, sanitized validation message:
   never API keys, evidence, prompts or private code.

All errors here are mocked. No network requests are made.
"""

from __future__ import annotations

import json
import re

import pytest

from portfolioforge.understanding import approve_manifest, build_manifest, submit
from portfolioforge.understanding.analysis import OUTPUT_SCHEMA, SYSTEM_PROMPT
from portfolioforge.understanding.anthropic_provider import (
    AnthropicProvider,
    ProviderRequestFailed,
    describe_api_error,
    sanitize_error_message,
)
from test_ai_analysis import FakeClient, no_network  # noqa: F401  (fixture)
from test_understanding import _pack, world  # noqa: F401  (fixture)

SUPPORTED_KEYWORDS = {"type", "properties", "required", "additionalProperties", "items", "enum"}


def _objects(node, depth=0, out=None):
    out = [] if out is None else out
    if isinstance(node, dict):
        if node.get("type") == "object":
            out.append((node, depth))
            depth += 1
        for child in node.get("properties", {}).values():
            _objects(child, depth, out)
        if "items" in node:
            _objects(node["items"], depth, out)
    return out


# --- schema shape ---------------------------------------------------------------------------


def test_schema_is_flat_enough_to_compile():
    objects = _objects(OUTPUT_SCHEMA)
    assert len(objects) <= 6, "keep the schema flat: nested object types make it harder to compile"
    assert max(depth for _, depth in objects) <= 1  # root, plus one level of item objects
    assert len(json.dumps(OUTPUT_SCHEMA)) < 4000


def test_schema_uses_only_supported_features():
    def check(node):
        if isinstance(node, dict):
            assert set(node) <= SUPPORTED_KEYWORDS, set(node) - SUPPORTED_KEYWORDS
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])  # no optional parameters
            if "enum" in node:
                assert all(isinstance(v, str) for v in node["enum"])
            for child in node.get("properties", {}).values():
                check(child)
            if "items" in node:
                check(node["items"])
    check(OUTPUT_SCHEMA)


def test_request_body_has_only_documented_fields(world):
    pack, _, _ = _pack(world)
    _, payload = build_manifest(pack, "anthropic", "claude-sonnet-5-5")
    body = json.loads(payload)
    assert set(body) == {"model", "max_tokens", "system", "output_config", "messages"}
    assert set(body["output_config"]) == {"effort", "format"}
    assert body["output_config"]["effort"] in {"low", "medium", "high", "xhigh", "max"}
    assert set(body["output_config"]["format"]) == {"type", "schema"}
    assert isinstance(body["system"], str) and isinstance(body["messages"][0]["content"], str)
    assert "thinking" not in body and "temperature" not in body


# --- sanitized error reporting ---------------------------------------------------------------


class FakeAPIError(Exception):
    """Shaped like the SDK's APIStatusError: status_code, body, request_id, message."""

    def __init__(self, status, message, error_type="invalid_request_error", request_id="req_011Synthetic"):
        super().__init__(message)
        self.status_code = status
        self.message = message
        self.request_id = request_id
        self.body = {"type": "error", "error": {"type": error_type, "message": message}}


class BadRequestError(FakeAPIError):
    pass


class APIConnectionError(Exception):
    pass


def _body(world):
    pack, _, _ = _pack(world)
    _, payload = build_manifest(pack, "anthropic", "claude-sonnet-5-5")
    return json.loads(payload), payload


def test_schema_too_complex_is_reported_usefully(world):
    body, _ = _body(world)
    error = describe_api_error(
        BadRequestError(400, "output_config.format.schema: Schema is too complex for compilation."), body)
    text = str(error)
    assert "BadRequestError (HTTP 400, invalid_request_error)" in text
    assert "Schema is too complex for compilation." in text
    assert "[request id req_011Synthetic]" in text
    assert error.status == 400 and error.error_type == "invalid_request_error"


def test_echoed_evidence_and_prompt_text_are_removed(world):
    body, _ = _body(world)
    content = body["messages"][0]["content"]
    excerpt = re.search(r'def train\([^"]{10,60}', content).group(0)
    prompt_words = " ".join(SYSTEM_PROMPT.split()[10:18])
    message = (f"messages.0.content: unexpected value '{excerpt}' near {prompt_words} in request")
    text = str(describe_api_error(BadRequestError(400, message), body))
    assert excerpt not in text and prompt_words not in text
    assert "[request content removed]" in text and "messages.0.content" in text


def test_keys_emails_and_paths_are_removed(world):
    body, _ = _body(world)
    key = "sk-ant-" + "api03-" + "A" * 24
    path = "C:" + "\\Users\\" + "someone\\secret.py"
    message = f"invalid x-api-key {key} for team@example.org, see {path}"
    text = sanitize_error_message(message, body)
    assert key not in text and "team@example.org" not in text and "someone" not in text
    assert "[credential removed]" in text


def test_long_messages_are_truncated(world):
    body, _ = _body(world)
    text = sanitize_error_message("x" * 5000, body)
    assert len(text) <= 301 and text.endswith("…")


def test_connection_errors_report_type_phase_and_message(world):
    body, _ = _body(world)
    text = str(describe_api_error(APIConnectionError("could not reach host"), body))
    assert text.startswith("Anthropic request failed: APIConnectionError during ")
    assert text.endswith(": could not reach host")


def test_untrusted_request_ids_are_not_shown(world):
    body, _ = _body(world)
    text = str(describe_api_error(BadRequestError(400, "bad", request_id="req_<script>"), body))
    assert "request id" not in text


def test_provider_raises_the_sanitized_error(world, no_network):
    body, payload = _body(world)
    pack, _, _ = _pack(world)
    manifest, payload = build_manifest(pack, "anthropic", "claude-sonnet-5-5")
    client = FakeClient(error=BadRequestError(400, "Schema is too complex for compilation."))
    with pytest.raises(ProviderRequestFailed) as excinfo:
        submit(manifest, payload, approve_manifest(manifest), AnthropicProvider(manifest.model, client))
    assert "Schema is too complex for compilation." in str(excinfo.value)
    assert excinfo.value.__cause__ is None  # the SDK exception (with request details) is not chained


def test_cli_reports_and_audits_only_the_sanitized_error(tmp_path, monkeypatch, fake_system, capsys, no_network):
    from conftest import make_tree
    from portfolioforge import paths
    from portfolioforge.grouping import cli as gcli
    from portfolioforge.grouping.store import load_run
    from portfolioforge.inventory import JsonlSink, plan_targeted, run_scan
    from portfolioforge.understanding import cli as ucli
    from test_understanding import _python_source

    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "pf-data"))
    root = make_tree(tmp_path / "Synthetic", {"Projects/SpamFilter/train.py": _python_source(tmp_path / "X.txt"),
                                               "Projects/SpamFilter/README.md": "# Spam Filter\n"})
    with JsonlSink(paths.inventories_dir() / "20260501T000000Z") as sink:
        run_scan(plan_targeted([root], system=fake_system), sink=sink)
    gcli.main(["propose"])
    group = next(g.id for g in load_run("20260501T000000Z").groups if g.working_title.text == "SpamFilter")
    gcli.main(["accept", group])
    for args in (["approve-files", group, "--yes"], ["extract", group],
                 ["manifest", group, "--provider", "anthropic"]):
        assert ucli.main(args) == 0
    manifest_id = re.search(r"Disclosure manifest (m_\w+)", capsys.readouterr().out).group(1)
    assert ucli.main(["approve-manifest", group, manifest_id, "--yes"]) == 0
    capsys.readouterr()

    echo = "Spam filter training script"
    client = FakeClient(error=BadRequestError(400, f"Schema is too complex for compilation. Saw '{echo}'"))
    monkeypatch.setattr(ucli, "make_provider", lambda name, model: AnthropicProvider(model, client))
    assert ucli.main(["submit", group, manifest_id, "--yes"]) == 1
    err = capsys.readouterr().err
    assert "HTTP 400, invalid_request_error" in err and "Schema is too complex" in err and echo not in err
    audit = (paths.data_dir() / "understanding" / "20260501T000000Z" / group / "audit.jsonl").read_text()
    assert '"outcome": "failed"' in audit and "Schema is too complex" in audit and echo not in audit
