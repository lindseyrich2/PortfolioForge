"""Anthropic provider adapter. Sends exactly the approved request body, nothing else.

The approved payload *is* the complete Messages API request body (model,
max_tokens, system prompt, output schema and the evidence message). This
adapter parses those bytes and passes the same fields to
``client.messages.create``. The SDK adds only transport headers
(authentication, API version); it adds no content.

Credentials are resolved by the Anthropic SDK in its usual order
(``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN``, an ``ant auth login``
profile), or from ``PORTFOLIOFORGE_ANTHROPIC_API_KEY`` if set. PortfolioForge
never stores, prints or logs credentials.

Server-side refusal fallbacks are deliberately NOT enabled: a fallback would let
a model other than the approved one process the content. A refusal stops the
request instead.

Requires the optional dependency: ``pip install -e ".[ai]"``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

from .models import ProviderResult
from .redact import redact

API_KEY_ENV = "PORTFOLIOFORGE_ANTHROPIC_API_KEY"
MAX_ERROR_MESSAGE_CHARS = 300
_KEY_LIKE = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{6,}|\b(?:x-api-key|authorization)\s*[:=]\s*\S+", re.IGNORECASE)
_QUOTED = re.compile(r"(['\"`])(.{1,2000}?)\1")


class ProviderRequestFailed(RuntimeError):
    """The provider call failed. The message is sanitized: no keys, evidence, prompts or code."""

    def __init__(self, message: str, *, status: int | None = None, error_type: str | None = None,
                 request_id: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.request_id = request_id


def sanitize_error_message(message: str, body: dict[str, Any]) -> str:
    """Make an API error message safe to show and log.

    Removes anything quoted that also occurs in the request (evidence, prompt or
    schema text), key-like strings, emails, paths and secrets, then truncates.
    """
    sent_text = "\n".join([str(body.get("system", ""))] +
                          [str(m.get("content", "")) for m in body.get("messages", []) if isinstance(m, dict)])

    def scrub_quoted(match: re.Match[str]) -> str:
        inner = match.group(2)
        if len(inner) >= 8 and inner in sent_text:
            return f"{match.group(1)}[request content removed]{match.group(1)}"
        return match.group(0)

    text = _QUOTED.sub(scrub_quoted, str(message)[:4 * MAX_ERROR_MESSAGE_CHARS])
    # Unquoted echoes: drop any window of 4+ consecutive words (20+ characters) found in the request.
    words = text.split()
    echoed = [False] * len(words)
    window = 4
    for start in range(0, max(len(words) - window + 1, 0)):
        piece = " ".join(words[start:start + window])
        if len(piece) >= 20 and piece in sent_text:
            for i in range(start, start + window):
                echoed[i] = True
    if any(echoed):
        kept, previous_removed = [], False
        for word, removed in zip(words, echoed):
            if removed:
                if not previous_removed:
                    kept.append("[request content removed]")
            else:
                kept.append(word)
            previous_removed = removed
        text = " ".join(kept)
    text = _KEY_LIKE.sub("[credential removed]", text)
    text, _ = redact(text)
    text = " ".join(text.split())
    return text if len(text) <= MAX_ERROR_MESSAGE_CHARS else text[:MAX_ERROR_MESSAGE_CHARS] + "…"


class SendTracker:
    """Records, through HTTP event hooks, whether a request actually left the computer."""

    def __init__(self) -> None:
        self.request_started = False
        self.response_received = False

    def hooks(self) -> dict[str, list]:
        def on_request(request: Any) -> None:
            self.request_started = True

        def on_response(response: Any) -> None:
            self.response_received = True

        return {"request": [on_request], "response": [on_response]}

    def describe(self) -> str:
        if self.response_received:
            return "the request was sent and a response was received"
        if self.request_started:
            return "the request may have been sent (the connection started)"
        return "nothing was sent to Anthropic"


#: Phase names by the module the exception was raised in (innermost matching frame wins).
_PHASES = (
    ("anthropic.lib.credentials", "credential resolution"),
    ("anthropic.lib.auth", "credential resolution"),
    ("httpcore", "network transport"),
    ("httpx2", "network transport"),
    ("anthropic._response", "response parsing"),
    ("anthropic._models", "response parsing"),
    ("anthropic.types", "response parsing"),
)
_SAFE_MODULE_PREFIXES = ("anthropic", "httpx2", "httpcore", "portfolioforge", "pydantic", "json")


def exception_origin(exc: BaseException) -> tuple[str, str | None]:
    """(phase, location) from the traceback, using module and function names only.

    Never reads frame locals or file contents, so no request data can leak.
    """
    # Follow the cause chain (the SDK wraps transport errors in its own exceptions); the
    # innermost exception decides the phase and location.
    chain, current = [], exc
    while current is not None and len(chain) < 6:
        chain.append(current)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    frames = []
    for item in chain:
        tb = item.__traceback__
        while tb is not None:
            frame = tb.tb_frame
            frames.append((str(frame.f_globals.get("__name__", "")), frame.f_code.co_name, tb.tb_lineno))
            tb = tb.tb_next
    phase = "request preparation"
    for module, _, _ in frames:
        for prefix, name in _PHASES:
            if module == prefix or module.startswith(prefix + "."):
                phase = name
    location = None
    for module, function, line in reversed(frames):
        if module.split(".")[0] in _SAFE_MODULE_PREFIXES:
            location = f"{module}.{function}:{line}"
            break
    return phase, location


def describe_api_error(exc: Exception, body: dict[str, Any],
                       tracker: SendTracker | None = None) -> ProviderRequestFailed:
    """Turn an SDK exception into a sanitized, useful error."""
    status = getattr(exc, "status_code", None)
    request_id = getattr(exc, "request_id", None)
    error_type, message = None, None
    payload = getattr(exc, "body", None)
    if isinstance(payload, dict):
        detail = payload.get("error") if isinstance(payload.get("error"), dict) else payload
        error_type = detail.get("type") if isinstance(detail.get("type"), str) else None
        message = detail.get("message") if isinstance(detail.get("message"), str) else None
    if message is None and status is not None:
        message = getattr(exc, "message", None)
    parts = [f"Anthropic request failed: {type(exc).__name__}"]
    if status:
        parts.append(f"(HTTP {status}{', ' + error_type if error_type else ''})")
    else:
        # Not an API response: say where it failed and whether anything was sent.
        phase, location = exception_origin(exc)
        parts.append(f"during {phase}" + (f" at {location}" if location else ""))
        if tracker is not None:
            parts.append(f"({tracker.describe()})")
        message = message or (str(exc.args[0]) if exc.args and isinstance(exc.args[0], str) else None)
    text = " ".join(parts)
    if message:
        text += f": {sanitize_error_message(_strip_reprs(message), body)}"
    if isinstance(request_id, str) and re.fullmatch(r"req_[A-Za-z0-9]+", request_id):
        text += f" [request id {request_id}]"
    if not status and exception_origin(exc)[0] == "credential resolution":
        text += (" Hint: check this terminal's credentials (set ANTHROPIC_API_KEY, or run `ant auth status`). "
                 "PortfolioForge never reads or stores them.")
    return ProviderRequestFailed(text, status=status, error_type=error_type,
                                 request_id=request_id if isinstance(request_id, str) else None)


def _strip_reprs(message: str) -> str:
    """Drop long quoted values from non-API exception messages (they may be data reprs)."""
    return re.sub(r"(['\"`]).{41,}?\1", r"\1[value removed]\1", message)


def _import_sdk() -> Any:
    try:
        import anthropic
    except ImportError as exc:
        from .disclosure import ProviderNotConfigured
        raise ProviderNotConfigured(
            "The Anthropic SDK is not installed. Install it with: pip install -e \".[ai]\""
        ) from exc
    return anthropic


#: Placeholder credential for the offline preflight (mock transport). Not a real key; never sent.
_PREFLIGHT_PLACEHOLDER = "offline-preflight-placeholder"

_PREFLIGHT_RESPONSE = {
    "id": "msg_preflight", "type": "message", "role": "assistant", "model": "preflight",
    "content": [{"type": "thinking", "thinking": "", "signature": "preflight"}, {"type": "text", "text": "{}"}],
    "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
}


def preflight(body: dict[str, Any]) -> None:
    """Run the exact approved body through the installed SDK offline.

    Uses an in-memory mock transport and a dummy key, so no network request is made and no
    credentials are read. Fails (before anything is sent) if the SDK rejects the request, or
    if the bytes it would send differ from the approved body in any way.
    """
    anthropic = _import_sdk()
    import httpx2

    captured: list[dict[str, Any]] = []

    def handler(request: Any) -> Any:
        captured.append(json.loads(request.content))
        return httpx2.Response(200, json={**_PREFLIGHT_RESPONSE, "model": body.get("model", "preflight")})

    client = anthropic.Anthropic(
        api_key=_PREFLIGHT_PLACEHOLDER, max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    try:
        client.messages.create(**body)
    except Exception as exc:
        phase, location = exception_origin(exc)
        detail = str(exc.args[0]) if exc.args and isinstance(exc.args[0], str) else ""
        raise ProviderRequestFailed(
            f"Anthropic request failed: {type(exc).__name__} in the offline SDK preflight during {phase}"
            + (f" at {location}" if location else "") + " (nothing was sent to Anthropic)"
            + (f": {sanitize_error_message(_strip_reprs(detail), body)}" if detail else "")
        ) from None
    if captured != [body]:
        from .disclosure import DisclosureNotApproved
        raise DisclosureNotApproved(
            "The installed SDK would send a request that differs from the approved one; nothing was sent."
        )


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, client: Any | None = None, *, transport: Any | None = None) -> None:
        self.model = model
        self._client = client
        """A fake client, injected in tests. When None, a real SDK client is created per send."""
        self._transport = transport
        """An httpx2 transport for the real SDK client (tests use a MockTransport; never set in normal use)."""

    def _make_client(self, tracker: "SendTracker") -> Any:
        anthropic = _import_sdk()
        key = os.environ.get(API_KEY_ENV)
        http_client = anthropic.DefaultHttpxClient(event_hooks=tracker.hooks(), **self._transport_kwargs())
        # No automatic retries: after a timeout or server error, the request may already have been
        # processed and charged. Whether to send it again is the user's decision.
        kwargs: dict[str, Any] = {"http_client": http_client, "max_retries": 0}
        if key:
            kwargs["api_key"] = key
        return anthropic.Anthropic(**kwargs)

    def _transport_kwargs(self) -> dict[str, Any]:
        return {"transport": self._transport} if self._transport is not None else {}

    def send(self, payload: bytes) -> ProviderResult:
        body = json.loads(payload)
        if body.get("model") != self.model:
            from .disclosure import DisclosureNotApproved
            raise DisclosureNotApproved("The payload names a different model than the approved one; nothing was sent.")
        digest = hashlib.sha256(payload).hexdigest()
        tracker = SendTracker()
        if self._client is None:
            # Offline check first: the installed SDK must accept the request and serialize exactly
            # the approved body. Uses a mock transport and a dummy key; nothing leaves the computer.
            preflight(body)
        try:
            client = self._client or self._make_client(tracker)
            response = client.messages.create(**body)
        except ProviderRequestFailed:
            raise
        except Exception as exc:  # map SDK errors to a sanitized, useful message
            raise describe_api_error(exc, body, tracker) from None
        stop = getattr(response, "stop_reason", None)
        request_id = getattr(response, "_request_id", None)
        if stop == "refusal":
            return ProviderResult(self.name, self.model, True, len(payload), digest, "refused", None)
        if stop == "max_tokens":
            return ProviderResult(self.name, self.model, True, len(payload), digest, "truncated", None)
        text = next((b.text for b in getattr(response, "content", []) if getattr(b, "type", None) == "text"), None)
        if text is None:
            return ProviderResult(self.name, self.model, True, len(payload), digest, "no_text", None)
        status = "completed" if request_id is None else f"completed:{request_id}"
        return ProviderResult(self.name, self.model, True, len(payload), digest, status, text)
