"""The only code in PortfolioForge that opens an original project file.

A file can be read only if it is listed in a FileApprovalRecord, is still the
same regular file (size and modification time unchanged, not a link, not a
cloud-only placeholder), is not sensitive or private, and is within the size
limit. Files are read as bytes and decoded as text; nothing is executed.
"""

from __future__ import annotations

import os
import stat
from datetime import datetime

from ..inventory import rules
from ..inventory.platform import CLOUD_FILE_ATTRIBUTES, file_attributes, is_link_stat
from ..inventory.roots import is_private_location
from ..privacy import PrivacySettings
from .models import ApprovedFile, FileApprovalRecord
from .selection import MAX_NOTEBOOK_BYTES

MTIME_TOLERANCE_SECONDS = 2.0


class ReadRefused(PermissionError):
    """Raised when a file may not be opened. Messages never include the path."""


class ApprovedFileReader:
    def __init__(self, approval: FileApprovalRecord, privacy: PrivacySettings,
                 max_bytes: int = MAX_NOTEBOOK_BYTES) -> None:
        self._approved = {f.file_id: f for f in approval.files}
        self._privacy = privacy
        self._max_bytes = max_bytes
        self.opened: list[str] = []
        """File ids actually opened, for auditing and tests."""

    def read_text(self, file_id: str) -> str:
        approved = self._approved.get(file_id)
        if approved is None:
            raise ReadRefused("This file is not in the approval record.")
        self._check(approved)
        with open(approved.absolute_path, "rb") as handle:
            data = handle.read(self._max_bytes + 1)
        self.opened.append(file_id)
        if len(data) > self._max_bytes:
            raise ReadRefused("File is larger than the reading limit.")
        return data.decode("utf-8", errors="replace")

    def _check(self, approved: ApprovedFile) -> None:
        name = os.path.basename(approved.absolute_path)
        if rules.is_sensitive_name(name):
            raise ReadRefused("Sensitive file names are never read.")
        if is_private_location(approved.absolute_path, self._privacy.private_paths,
                               self._privacy.private_folder_names):
            raise ReadRefused("Files in private folders are never read.")
        try:
            st = os.lstat(approved.absolute_path)
        except OSError as exc:
            raise ReadRefused(f"File is no longer available ({type(exc).__name__}).") from None
        if is_link_stat(st) or not stat.S_ISREG(st.st_mode):
            raise ReadRefused("Only regular files are read (no links or special files).")
        if file_attributes(st) & CLOUD_FILE_ATTRIBUTES:
            raise ReadRefused("Cloud-only placeholders are never read (that would download them).")
        if st.st_size != approved.size_bytes:
            raise ReadRefused("File changed since it was approved (size differs). Approve it again.")
        if approved.modified_at:
            approved_time = datetime.fromisoformat(approved.modified_at).timestamp()
            if abs(st.st_mtime - approved_time) > MTIME_TOLERANCE_SECONDS:
                raise ReadRefused("File changed since it was approved (modified). Approve it again.")
