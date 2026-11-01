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
from typing import Any

from .models import ProviderResult

API_KEY_ENV = "PORTFOLIOFORGE_ANTHROPIC_API_KEY"


class ProviderRequestFailed(RuntimeError):
    """The provider call failed. Messages carry the error type and status only."""


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
        except Exception as exc:  # map SDK errors without echoing request content
            status = getattr(exc, "status_code", None)
            raise ProviderRequestFailed(
                f"Anthropic request failed: {type(exc).__name__}" + (f" (HTTP {status})" if status else "")
            ) from None
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
