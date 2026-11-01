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


def describe_api_error(exc: Exception, body: dict[str, Any]) -> ProviderRequestFailed:
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
    text = " ".join(parts)
    if message:
        text += f": {sanitize_error_message(message, body)}"
    if isinstance(request_id, str) and re.fullmatch(r"req_[A-Za-z0-9]+", request_id):
        text += f" [request id {request_id}]"
    return ProviderRequestFailed(text, status=status, error_type=error_type,
                                 request_id=request_id if isinstance(request_id, str) else None)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, client: Any | None = None) -> None:
        self.model = model
        self._client = client
        """Injected in tests. When None, a real SDK client is created on first send."""

    def _make_client(self) -> Any:
        try:
            import anthropic
        except ImportError as exc:
            from .disclosure import ProviderNotConfigured
            raise ProviderNotConfigured(
                "The Anthropic SDK is not installed. Install it with: pip install -e \".[ai]\""
            ) from exc
        key = os.environ.get(API_KEY_ENV)
        return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()

    def send(self, payload: bytes) -> ProviderResult:
        body = json.loads(payload)
        if body.get("model") != self.model:
            from .disclosure import DisclosureNotApproved
            raise DisclosureNotApproved("The payload names a different model than the approved one; nothing was sent.")
        client = self._client or self._make_client()
        digest = hashlib.sha256(payload).hexdigest()
        try:
            response = client.messages.create(**body)
        except Exception as exc:  # map SDK errors to a sanitized, useful message
            raise describe_api_error(exc, body) from None
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
