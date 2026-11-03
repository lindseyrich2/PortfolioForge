"""A pretend Google: an in-memory Drive behind the client's transport. Every name and file is invented.

No test here talks to Google or looks at a real Drive. Anything but a GET to the Drive files endpoint
fails the test, so these tests also check that discovery can't change anything.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass, field

from portfolioforge.drive.auth import CONTENT_SCOPE, METADATA_SCOPE, Credentials, HttpResponse, OAuthClient
from portfolioforge.drive.client import API_BASE, COLAB_MIME, FOLDER_MIME, SHORTCUT_MIME, DriveClient

ROOT_ID = "rootFolder0"
DOC_MIME = "application/vnd.google-apps.document"
__all__ = ["COLAB_MIME", "DOC_MIME", "FOLDER_MIME", "ROOT_ID", "SHORTCUT_MIME", "FakeDrive", "credentials", "notebook"]


def notebook(text: str = "print(1)") -> str:
    return json.dumps({"cells": [{"cell_type": "code", "source": [text], "outputs": []}], "metadata": {},
                       "nbformat": 4, "nbformat_minor": 5})


def credentials(scope: str = METADATA_SCOPE, transport=None) -> Credentials:
    return Credentials(scope, OAuthClient("synthetic-client"), {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh",
                                                                 "expires_at": 9e12, "scope": scope},
                       transport=transport or (lambda *a: (_ for _ in ()).throw(AssertionError("no refresh expected"))))


@dataclass
class FakeDrive:
    items: dict[str, dict] = field(default_factory=dict)
    contents: dict[str, bytes] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    page_size: int = 3
    fail_after: int | None = None
    responses: list[HttpResponse] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.items[ROOT_ID] = {"id": ROOT_ID, "name": "My Drive", "mimeType": FOLDER_MIME, "ownedByMe": True}

    def folder(self, drive_id: str, name: str, parent: str = ROOT_ID, **extra) -> str:
        self.items[drive_id] = {"id": drive_id, "name": name, "mimeType": FOLDER_MIME, "parents": [parent],
                                "ownedByMe": True, **extra}
        return drive_id

    def file(self, drive_id: str, name: str, parent: str = ROOT_ID, content: str | None = "", *,
             mime: str = "text/plain", modified: str = "2024-03-01T10:00:00.000Z", **extra) -> str:
        item = {"id": drive_id, "name": name, "mimeType": mime, "parents": [parent], "ownedByMe": True,
                "modifiedTime": modified, "createdTime": "2024-01-01T10:00:00.000Z"}
        if content is not None:
            data = content.encode("utf-8")
            self.contents[drive_id] = data
            item.update(size=str(len(data)), md5Checksum=hashlib.md5(data, usedforsecurity=False).hexdigest())
        self.items[drive_id] = {**item, **extra}
        return drive_id

    @property
    def downloads(self) -> list[str]:
        return [c for c in self.calls if "alt=media" in c]

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None, max_bytes: int) -> HttpResponse:
        assert method == "GET", f"Drive was sent a {method}"
        assert body is None
        assert url.startswith(API_BASE), "a request went somewhere other than the Drive files endpoint"
        assert headers["Authorization"].startswith("Bearer ")
        self.calls.append(url)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            return HttpResponse(500, b"{}")
        if self.responses:
            return self.responses.pop(0)
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query)
        path = parts.path[len(urllib.parse.urlsplit(API_BASE).path):].lstrip("/")
        if not path:
            return self._list(query)
        file_id = ROOT_ID if path == "root" else urllib.parse.unquote(path)
        if file_id not in self.items:
            return HttpResponse(404, b"{}")
        if query.get("alt") == ["media"]:
            assert headers["Authorization"] == "Bearer synthetic-access"
            return HttpResponse(200, self.contents[file_id][:max_bytes + 1])
        return HttpResponse(200, json.dumps(self.items[file_id]).encode())

    def _list(self, params: dict) -> HttpResponse:
        assert params["corpora"] == ["user"], "shared drives must stay out"
        q = params["q"][0]
        matching = [i for i in self.items.values() if i["id"] != ROOT_ID and _matches(i, q)]
        start = int(params.get("pageToken", ["0"])[0])
        page = {"files": matching[start:start + self.page_size]}
        if start + self.page_size < len(matching):
            page["nextPageToken"] = str(start + self.page_size)
        return HttpResponse(200, json.dumps(page).encode())


_STRING = r"'((?:[^'\\]|\\.)*)'"


def _matches(item: dict, query: str) -> bool:
    """Just enough of Drive's search language for the queries the client sends."""
    rest = query
    if name := re.search(rf"name = {_STRING} and ", rest):
        if item["name"] != name.group(1).replace("\\'", "'").replace("\\\\", "\\"):
            return False
        rest = rest.replace(name.group(0), "")
    for clause in rest.split(" and "):
        if clause == "trashed = false":
            if item.get("trashed"):
                return False
        elif clause == "'me' in owners":
            if not item.get("ownedByMe", True):
                return False
        elif parent := re.fullmatch(rf"{_STRING} in parents", clause):
            if parent.group(1) not in item.get("parents", ()):
                return False
        elif mime := re.fullmatch(rf"mimeType = {_STRING}", clause):
            if item["mimeType"] != mime.group(1):
                return False
        else:
            raise AssertionError(f"the fake doesn't understand this query clause: {clause}")
    return True


def client_for(drive: FakeDrive, scope: str = METADATA_SCOPE) -> DriveClient:
    return DriveClient(credentials(scope), transport=drive, sleep=lambda seconds: None)


def content_client_for(drive: FakeDrive) -> DriveClient:
    return client_for(drive, CONTENT_SCOPE)
