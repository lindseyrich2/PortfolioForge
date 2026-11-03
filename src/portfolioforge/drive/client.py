"""The Drive API, read-only by construction.

Every Drive request goes through :meth:`DriveClient._get`, which can only issue a GET to the files
endpoints. Nothing in this module can create, change, share, move or delete anything.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterator

from .auth import CONTENT_SCOPE, Credentials, DriveAuthError, DriveError, HttpResponse, Transport, urllib_transport

API_BASE = "https://www.googleapis.com/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
NATIVE_MIME_PREFIX = "application/vnd.google-apps."
COLAB_MIME = "application/vnd.google.colaboratory"
NOTEBOOK_MIMES = frozenset({COLAB_MIME, "application/x-ipynb+json"})
_FIELDS = "id,name,mimeType,parents,size,md5Checksum,modifiedTime,createdTime,ownedByMe"
_MAX_LISTING_BYTES = 50_000_000
_MAX_RETRIES = 4
_RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
_ID = re.compile(r"[A-Za-z0-9_-]+\Z")


@dataclass(frozen=True, slots=True)
class DriveEntry:
    id: str
    name: str
    mime_type: str
    parents: tuple[str, ...] = ()
    size: int | None = None
    md5: str | None = None
    modified_at: datetime | None = None
    created_at: datetime | None = None
    owned_by_me: bool = True

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME

    @classmethod
    def from_api(cls, d: dict) -> DriveEntry:
        size = d.get("size")
        return cls(d["id"], d.get("name", ""), d.get("mimeType", ""), tuple(d.get("parents", ())),
                   int(size) if size is not None else None, d.get("md5Checksum"), _time(d.get("modifiedTime")),
                   _time(d.get("createdTime")), d.get("ownedByMe", True))


def _time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def is_drive_id(value: str) -> bool:
    return bool(_ID.match(value))


def quote_query(value: str) -> str:
    """A value for a single-quoted string in a Drive search query, so a name can't change the query."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


class DriveClient:
    def __init__(self, credentials: Credentials, *, transport: Transport = urllib_transport,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._credentials, self._transport, self._sleep = credentials, transport, sleep

    def _get(self, path: str, params: dict[str, str], max_bytes: int) -> bytes:
        url = API_BASE + path + "?" + urllib.parse.urlencode(params)
        refreshed = False
        for attempt in range(_MAX_RETRIES + 1):
            response = self._transport("GET", url, {"Authorization": f"Bearer {self._credentials.bearer()}"}, None, max_bytes)
            if response.status == 200:
                return response.body
            if response.status == 401 and not refreshed:
                self._credentials.refresh()
                refreshed = True
                continue
            if not _is_retryable(response) or attempt == _MAX_RETRIES:
                break
            self._sleep(2.0 ** attempt)
        raise _failure(response)

    def get(self, file_id: str) -> DriveEntry:
        if file_id != "root" and not is_drive_id(file_id):
            raise DriveError("That is not a Drive ID.")
        body = self._get(f"/{urllib.parse.quote(file_id, safe='')}", {"fields": _FIELDS}, _MAX_LISTING_BYTES)
        return DriveEntry.from_api(json.loads(body))

    def list(self, query: str) -> Iterator[DriveEntry]:
        """Every match, page by page. Only My Drive and items shared with you, never shared drives."""
        params = {"q": query, "corpora": "user", "spaces": "drive", "pageSize": "1000",
                  "fields": f"nextPageToken,files({_FIELDS})"}
        while True:
            page = json.loads(self._get("", params, _MAX_LISTING_BYTES))
            for item in page.get("files", ()):
                yield DriveEntry.from_api(item)
            if not page.get("nextPageToken"):
                return
            params = {**params, "pageToken": page["nextPageToken"]}

    def download(self, file_id: str, max_bytes: int) -> bytes:
        """A file's bytes. Refused outright unless this client holds the separate download permission."""
        if self._credentials.scope != CONTENT_SCOPE:
            raise DriveAuthError("The discovery permission cannot download files.")
        if not is_drive_id(file_id):
            raise DriveError("That is not a Drive ID.")
        return self._get(f"/{urllib.parse.quote(file_id, safe='')}", {"alt": "media"}, max_bytes)


def _reasons(response: HttpResponse) -> set[str]:
    try:
        return {e.get("reason") for e in json.loads(response.body)["error"]["errors"]}
    except (ValueError, KeyError, TypeError, AttributeError):
        return set()


def _is_retryable(response: HttpResponse) -> bool:
    if response.status in (429, 500, 502, 503, 504):
        return True
    return response.status == 403 and bool(_reasons(response) & _RATE_LIMIT_REASONS)


def _failure(response: HttpResponse) -> DriveError:
    if response.status == 401:
        return DriveAuthError("Google no longer accepts this sign-in. Connect again with: "
                              "python -m portfolioforge.drive connect")
    if response.status == 403:
        return DriveError("Google refused the request (HTTP 403). Check that the Drive API is enabled for your OAuth "
                          "client, and that you allowed the requested read-only permission.")
    if response.status == 404:
        return DriveError("Google could not find that item (HTTP 404). It may have been moved or deleted.")
    return DriveError(f"The Drive request failed (HTTP {response.status}).")
