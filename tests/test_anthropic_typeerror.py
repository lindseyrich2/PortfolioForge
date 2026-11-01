"""Regression tests for the opaque `Anthropic request failed: TypeError` from a real submission.

The old handler reported only the exception class. These tests reproduce a
TypeError raised inside the SDK call (credential resolution, request
preparation, transport, response parsing) and check that the report now says
where it happened and whether anything was sent, without leaking request data.

The "real SDK" tests run the installed Anthropic SDK against an in-memory mock
transport with a dummy key: no network, no credentials. They are skipped when
the optional SDK is not installed (as in CI).
"""

from __future__ import annotations

import json

import pytest

from portfolioforge.understanding import approve_manifest, build_manifest, submit
from portfolioforge.understanding.anthropic_provider import (
    AnthropicProvider,
    ProviderRequestFailed,
    SendTracker,
    describe_api_error,
    exception_origin,
    preflight,
)
from portfolioforge.understanding.disclosure import DisclosureNotApproved
from test_ai_analysis import FakeClient, no_network  # noqa: F401  (fixture)
from test_understanding import _pack, world  # noqa: F401  (fixture)


def _raiser(module: str, message: str, exc_type: type[BaseException] = TypeError):
    """A function whose frames report `module` as their module name, like SDK internals do."""
    namespace = {"__name__": module, "exc_type": exc_type, "message": message}
    exec("def boom(*args, **kwargs):\n    raise exc_type(message)\n", namespace)
    return namespace["boom"]


class RaisingClient:
    def __init__(self, func):
        self.messages = type("M", (), {"create": staticmethod(func)})()


def _manifest(world):
    pack, _, _ = _pack(world)
    return build_manifest(pack, "anthropic", "claude-sonnet-5-5")


@pytest.mark.parametrize(("module", "phase"), [
    ("anthropic.lib.credentials._secrets", "credential resolution"),
    ("anthropic._utils._transform", "request preparation"),
    ("httpx2._transports.default", "network transport"),
    ("anthropic._response", "response parsing"),
])
def test_type_errors_report_their_phase_and_location(world, no_network, module, phase):
    manifest, payload = _manifest(world)
    client = RaisingClient(_raiser(module, "Object of type SecretStr is not JSON serializable"))
    with pytest.raises(ProviderRequestFailed) as excinfo:
        submit(manifest, payload, approve_manifest(manifest), AnthropicProvider(manifest.model, client))
    text = str(excinfo.value)
    assert text != "Anthropic request failed: TypeError"  # the old, opaque report
    assert f"TypeError during {phase} at {module}.boom:" in text
    assert "Object of type SecretStr is not JSON serializable" in text
    assert "nothing was sent to Anthropic" in text
    assert ("Hint: check this terminal's credentials" in text) is (phase == "credential resolution")


def test_diagnostics_never_include_request_data(world, no_network):
    manifest, payload = _manifest(world)
    body = json.loads(payload)
    excerpt = body["messages"][0]["content"][60:140]
    long_repr = "'" + "private value " * 6 + "'"
    client = RaisingClient(_raiser("anthropic._utils._transform", f"bad value {long_repr} near '{excerpt}'"))
    with pytest.raises(ProviderRequestFailed) as excinfo:
        submit(manifest, payload, approve_manifest(manifest), AnthropicProvider(manifest.model, client))
    text = str(excinfo.value)
    assert excerpt not in text and "private value" not in text
    assert excinfo.value.__cause__ is None


def test_wrapped_transport_errors_use_the_innermost_cause(world):
    body = json.loads(_manifest(world)[1])
    inner = _raiser("httpcore._sync.connection", "socket closed", OSError)
    outer_namespace = {"__name__": "anthropic._base_client", "inner": inner}
    exec("class APIConnectionError(Exception):\n    pass\n"
         "def call():\n    try:\n        inner()\n    except OSError as e:\n"
         "        raise APIConnectionError('Connection error.') from e\n", outer_namespace)
    with pytest.raises(Exception) as excinfo:
        outer_namespace["call"]()
    phase, location = exception_origin(excinfo.value)
    assert phase == "network transport" and location.startswith("httpcore._sync.connection.boom")
    tracker = SendTracker()
    tracker.request_started = True
    text = str(describe_api_error(excinfo.value, body, tracker))
    assert "APIConnectionError during network transport" in text and "may have been sent" in text


# --- the installed SDK, offline (mock transport, dummy key) ----------------------------------------


@pytest.fixture
def sdk():
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    return anthropic, httpx2


def _mock(httpx2, seen, text="{}"):
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json={
            "id": "msg_mock", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [{"type": "thinking", "thinking": "", "signature": "s"}, {"type": "text", "text": text}],
            "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})
    return httpx2.MockTransport(handler)


def test_real_sdk_path_sends_exactly_the_approved_body(world, sdk, no_network, monkeypatch):
    _, httpx2 = sdk
    monkeypatch.setenv("PORTFOLIOFORGE_ANTHROPIC_API_KEY", "dummy-test-key-not-real")
    manifest, payload = _manifest(world)
    seen: list[dict] = []
    provider = AnthropicProvider(manifest.model, transport=_mock(httpx2, seen))
    result = submit(manifest, payload, approve_manifest(manifest), provider)
    assert seen == [json.loads(payload)]  # the real SDK put exactly the approved body on the wire
    assert result.sent and result.response == "{}"


def test_preflight_accepts_the_approved_request(world, sdk):
    _, payload = _manifest(world)
    preflight(json.loads(payload))  # offline: mock transport, dummy key


def test_preflight_catches_sdk_rejections_before_sending(world, sdk, no_network):
    manifest, payload = _manifest(world)
    bad = json.loads(payload)
    bad["unexpected_field"] = True
    with pytest.raises(ProviderRequestFailed) as excinfo:
        AnthropicProvider(manifest.model).send(json.dumps(bad).encode())
    text = str(excinfo.value)
    assert "TypeError in the offline SDK preflight" in text and "nothing was sent to Anthropic" in text


def test_preflight_refuses_if_the_sdk_would_change_the_body(world, sdk, monkeypatch):
    anthropic, _ = sdk
    _, payload = _manifest(world)
    original = anthropic.resources.messages.Messages.create

    def altering_create(self, **kwargs):
        kwargs["max_tokens"] = kwargs["max_tokens"] + 1
        return original(self, **kwargs)

    monkeypatch.setattr(anthropic.resources.messages.Messages, "create", altering_create)
    with pytest.raises(DisclosureNotApproved, match="differs from the approved one"):
        preflight(json.loads(payload))


def test_real_sdk_transport_failure_reports_whether_anything_was_sent(world, sdk, no_network, monkeypatch):
    _, httpx2 = sdk
    monkeypatch.setenv("PORTFOLIOFORGE_ANTHROPIC_API_KEY", "dummy-test-key-not-real")
    manifest, payload = _manifest(world)

    def broken(request):
        raise TypeError("transport exploded")

    provider = AnthropicProvider(manifest.model, transport=httpx2.MockTransport(broken))
    with pytest.raises(ProviderRequestFailed) as excinfo:
        submit(manifest, payload, approve_manifest(manifest), provider)
    text = str(excinfo.value)
    assert "network transport" in text or "TypeError" in text
    assert "may have been sent" in text  # the request hook fired before the transport failed
