"""Google sign-in for an installed app: loopback redirect with PKCE. Standard library only.

There are exactly two permissions, each with its own token, and neither can change anything in Drive:

* ``drive.metadata.readonly`` for discovery: names, sizes, dates and checksums. It cannot download.
* ``drive.readonly`` for downloading files you have approved. Requested only by ``connect --content``.

Users bring their own OAuth client, so no shared credentials ship with the app. Messages and
exceptions never contain tokens or authorization codes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Protocol

from ..paths import config_dir

METADATA_SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"
CONTENT_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_TOKEN_NAMES = {METADATA_SCOPE: "google-drive-metadata.json", CONTENT_SCOPE: "google-drive-content.json"}
CLIENT_FILE_NAME = "google-oauth-client.json"
SIGN_IN_TIMEOUT_SECONDS = 300.0
_EXPIRY_MARGIN_SECONDS = 60.0
_MAX_RESPONSE_BYTES = 1_000_000


class DriveError(ValueError):
    """Anything that stops Drive work. Messages never contain paths, file names or tokens."""


class DriveAuthError(DriveError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


class Transport(Protocol):
    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None,
                 max_bytes: int) -> HttpResponse: ...


class _GoogleOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect keeps its headers, including the token, so it may only go to Google over HTTPS."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        host = parts.hostname or ""
        if parts.scheme != "https" or not host.endswith((".googleapis.com", ".googleusercontent.com")):
            raise DriveError("Google redirected a request somewhere unexpected, so it was stopped.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_GoogleOnlyRedirects())


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None,
                     max_bytes: int) -> HttpResponse:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _opener.open(request, timeout=60) as response:
            return HttpResponse(response.status, response.read(max_bytes + 1))
    except urllib.error.HTTPError as exc:
        with exc:
            return HttpResponse(exc.code, exc.read(_MAX_RESPONSE_BYTES))
    except (urllib.error.URLError, OSError) as exc:
        raise DriveError(f"Could not reach Google ({type(exc).__name__}).") from None


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str | None = None
    """Google treats a desktop client's secret as not confidential; sent only when the client file has one."""


def client_file() -> Path:
    return config_dir() / CLIENT_FILE_NAME


def token_file(scope: str) -> Path:
    return config_dir() / _TOKEN_NAMES[scope]


def _write_private(path: Path, data: dict) -> None:
    """Readable only by this user where the platform supports it. Replaced atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(data, stream)
    os.replace(temporary, path)


def parse_client(text: str) -> OAuthClient:
    """Read a client file downloaded from Google Cloud. Only a desktop ("installed") client is accepted."""
    try:
        section = json.loads(text)["installed"]
        return OAuthClient(section["client_id"], section.get("client_secret"))
    except (ValueError, KeyError, TypeError):
        raise DriveAuthError("That is not a desktop-app OAuth client file from Google Cloud.") from None


def save_client(source: str | os.PathLike[str]) -> OAuthClient:
    try:
        text = Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DriveAuthError(f"Could not read the OAuth client file ({type(exc).__name__}).") from None
    client = parse_client(text)
    _write_private(client_file(), json.loads(text))
    return client


def load_client() -> OAuthClient:
    try:
        text = client_file().read_text(encoding="utf-8")
    except OSError:
        raise DriveAuthError("No Google OAuth client is set up. See the README's Google Drive section, then run: "
                             "python -m portfolioforge.drive connect --client-file FILE") from None
    return parse_client(text)


def _form(values: dict[str, str]) -> bytes:
    return urllib.parse.urlencode(values).encode("ascii")


def _token_request(transport: Transport, values: dict[str, str]) -> dict:
    response = transport("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, _form(values),
                         _MAX_RESPONSE_BYTES)
    try:
        body = json.loads(response.body)
    except ValueError:
        body = {}
    if response.status != 200 or not isinstance(body, dict) or "access_token" not in body:
        reason = body.get("error") if isinstance(body, dict) else None
        if reason == "invalid_grant":
            raise DriveAuthError("Google no longer accepts this sign-in. Connect again with: "
                                 "python -m portfolioforge.drive connect")
        raise DriveAuthError(f"Google refused the token request (HTTP {response.status}).")
    return body


class Credentials:
    """One permission's token, refreshed when needed. Always holds exactly that one scope."""

    def __init__(self, scope: str, client: OAuthClient, data: dict, *, transport: Transport = urllib_transport,
                 clock: Callable[[], float] = time.time) -> None:
        self.scope, self._client, self._data, self._transport, self._clock = scope, client, data, transport, clock

    def bearer(self) -> str:
        if self._clock() >= self._data.get("expires_at", 0) - _EXPIRY_MARGIN_SECONDS:
            self.refresh()
        return self._data["access_token"]

    def refresh(self) -> None:
        refresh_token = self._data.get("refresh_token")
        if not refresh_token:
            raise DriveAuthError("This sign-in has expired. Connect again with: python -m portfolioforge.drive connect")
        values = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": self._client.client_id}
        if self._client.client_secret:
            values["client_secret"] = self._client.client_secret
        body = _token_request(self._transport, values)
        _require_scope(body.get("scope", self.scope), self.scope)
        self._data = {**self._data, "access_token": body["access_token"],
                      "expires_at": self._clock() + float(body.get("expires_in", 3600))}
        _write_private(token_file(self.scope), self._data)


def _require_scope(granted: str, wanted: str) -> None:
    """Refuse a token that holds anything but the one permission asked for, wider or narrower."""
    if set(granted.split()) != {wanted}:
        raise DriveAuthError("Google granted different permissions than the single read-only one requested, so the "
                             "sign-in was discarded. Connect again and allow only what is asked for.")


def load_credentials(scope: str, *, transport: Transport = urllib_transport) -> Credentials:
    try:
        data = json.loads(token_file(scope).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        hint = "connect --content" if scope == CONTENT_SCOPE else "connect"
        what = "download approved files" if scope == CONTENT_SCOPE else "list your Drive"
        raise DriveAuthError(f"PortfolioForge does not have permission to {what}. Grant it with: "
                             f"python -m portfolioforge.drive {hint}") from None
    _require_scope(data.get("scope", ""), scope)
    return Credentials(scope, load_client(), data, transport=transport)


def is_connected(scope: str) -> bool:
    return token_file(scope).is_file()


def disconnect(*, revoke: bool = False, transport: Transport = urllib_transport) -> int:
    """Delete local tokens, and optionally cancel them at Google. Returns how many were removed."""
    removed = 0
    for scope in _TOKEN_NAMES:
        path = token_file(scope)
        if not path.is_file():
            continue
        if revoke:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            token = data.get("refresh_token") or data.get("access_token")
            if token:
                response = transport("POST", REVOKE_URL, {"Content-Type": "application/x-www-form-urlencoded"},
                                     _form({"token": token}), _MAX_RESPONSE_BYTES)
                if response.status != 200:
                    raise DriveAuthError(f"Google did not confirm the revocation (HTTP {response.status}). "
                                         "The local sign-in was kept, so you can try again.")
        path.unlink()
        removed += 1
    return removed


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorization_url(client: OAuthClient, scope: str, redirect_uri: str, state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client.client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": scope,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })


def _callback_server(state: str, outcome: dict[str, str]) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the name http.server requires
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            matches = query.get("state", [""])[0] == state  # anything else, such as a favicon request, is ignored
            if matches:
                outcome.update({k: query[k][0] for k in ("code", "error") if k in query})
            self.send_response(200 if matches else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"PortfolioForge received the response. You can close this tab." if matches else b"Ignored.")

        def log_message(self, format: str, *args: object) -> None:  # the request line holds the code
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 0.5
    return server


def sign_in(scope: str, client: OAuthClient, *, transport: Transport = urllib_transport,
            open_browser: Callable[[str], object] = webbrowser.open, announce: Callable[[str], None] = print,
            timeout: float = SIGN_IN_TIMEOUT_SECONDS, clock: Callable[[], float] = time.monotonic) -> Credentials:
    """Ask Google for one permission in the user's browser, verify what was granted, and store the token."""
    state, (verifier, challenge) = secrets.token_urlsafe(32), _pkce_pair()
    outcome: dict[str, str] = {}
    server = _callback_server(state, outcome)
    try:
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"
        url = authorization_url(client, scope, redirect_uri, state, challenge)
        announce(f"Opening your browser to sign in. If it does not open, visit:\n{url}")
        open_browser(url)
        deadline = clock() + timeout
        while not outcome and clock() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not outcome:
        raise DriveAuthError("Timed out waiting for Google sign-in. Nothing was stored.")
    if "code" not in outcome:
        raise DriveAuthError("Google sign-in was cancelled or refused. Nothing was stored.")
    values = {"grant_type": "authorization_code", "code": outcome["code"], "client_id": client.client_id,
              "redirect_uri": redirect_uri, "code_verifier": verifier}
    if client.client_secret:
        values["client_secret"] = client.client_secret
    body = _token_request(transport, values)
    _require_scope(body.get("scope", ""), scope)
    data = {"scope": scope, "access_token": body["access_token"], "refresh_token": body.get("refresh_token"),
            "expires_at": time.time() + float(body.get("expires_in", 3600))}
    _write_private(token_file(scope), data)
    return Credentials(scope, client, data, transport=transport)
