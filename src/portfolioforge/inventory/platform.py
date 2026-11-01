"""Operating-system details: drives, known folders, file attributes, priority.

Everything here is metadata-only. Windows APIs are called through ctypes; on
other platforms the functions degrade to the home directory only.
"""

from __future__ import annotations

import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterator, Mapping

# Windows file attribute flags (as reported in os.stat_result.st_file_attributes).
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000

#: A file with any of these is a cloud-only placeholder: reading it would
#: download it. Such files are counted but never inventoried.
CLOUD_FILE_ATTRIBUTES = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
#: A folder with any of these is a cloud-only placeholder: listing it would
#: fetch its contents from the cloud. Such folders are never entered.
CLOUD_DIR_ATTRIBUTES = (
    FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # directory junction
IO_REPARSE_TAG_SYMLINK = 0xA000000C
IO_REPARSE_TAG_APPEXECLINK = 0x8000001B
_LINK_REPARSE_TAGS = frozenset(
    {IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_APPEXECLINK}
)


class DriveType(StrEnum):
    FIXED = "fixed"
    REMOVABLE = "removable"
    NETWORK = "network"
    OPTICAL = "optical"
    RAMDISK = "ramdisk"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DriveInfo:
    letter: str
    root: str
    drive_type: DriveType
    label: str | None = None
    filesystem: str | None = None


@dataclass(frozen=True, slots=True)
class SystemInfo:
    """The parts of the local system that root discovery needs.

    Tests construct this directly to describe a fake computer.
    """

    home: str
    system_drive: str | None = None
    drives: tuple[DriveInfo, ...] = ()
    known_folders: Mapping[str, str] = field(default_factory=dict)
    """Display name → path for Desktop, Documents, Downloads, and so on."""


def file_attributes(st: os.stat_result) -> int:
    return getattr(st, "st_file_attributes", 0)


def is_link_stat(st: os.stat_result) -> bool:
    """True for symlinks, junctions and app-execution links (from an lstat result)."""
    if stat.S_ISLNK(st.st_mode):
        return True
    return getattr(st, "st_reparse_tag", 0) in _LINK_REPARSE_TAGS


def is_link_path(path: str) -> bool:
    try:
        return is_link_stat(os.lstat(path))
    except OSError:
        return False


def detect_system() -> SystemInfo:
    """Describe this computer. Lists drives and known folders only."""
    home = os.path.expanduser("~")
    if os.name != "nt":
        return SystemInfo(home=home)
    system_drive = os.environ.get("SystemDrive", "C:").rstrip("\\/") + "\\"
    return SystemInfo(
        home=home,
        system_drive=system_drive,
        drives=_windows_drives(),
        known_folders=_windows_known_folders(),
    )


# --- Windows implementation ----------------------------------------------------

_DRIVE_TYPES = {
    2: DriveType.REMOVABLE,
    3: DriveType.FIXED,
    4: DriveType.NETWORK,
    5: DriveType.OPTICAL,
    6: DriveType.RAMDISK,
}

_KNOWN_FOLDER_IDS = {
    "Desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "Documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "Downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "Pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "Videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
    "Music": "4BD8D571-6D19-48D3-BE97-422220080E43",
}


def _windows_drives() -> tuple[DriveInfo, ...]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, wintypes.LPDWORD,
        wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPWSTR, wintypes.DWORD,
    ]
    sem_failcriticalerrors, sem_noopenfileerrorbox = 0x0001, 0x8000
    previous_mode = kernel32.SetErrorMode(sem_failcriticalerrors | sem_noopenfileerrorbox)
    drives: list[DriveInfo] = []
    try:
        mask = kernel32.GetLogicalDrives()
        for index in range(26):
            if not mask & (1 << index):
                continue
            letter = chr(ord("A") + index)
            root = f"{letter}:\\"
            drive_type = _DRIVE_TYPES.get(kernel32.GetDriveTypeW(root), DriveType.UNKNOWN)
            label = filesystem = None
            # Only query local fixed drives; querying others can touch the network
            # or wait on removable media.
            if drive_type is DriveType.FIXED:
                label_buf = ctypes.create_unicode_buffer(261)
                fs_buf = ctypes.create_unicode_buffer(261)
                if kernel32.GetVolumeInformationW(root, label_buf, 261, None, None, None, fs_buf, 261):
                    label = label_buf.value or None
                    filesystem = fs_buf.value or None
            drives.append(DriveInfo(letter, root, drive_type, label, filesystem))
    finally:
        kernel32.SetErrorMode(previous_mode)
    return tuple(drives)


def _windows_known_folders() -> dict[str, str]:
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    folders: dict[str, str] = {}
    for name, guid_text in _KNOWN_FOLDER_IDS.items():
        guid = GUID.from_buffer_copy(uuid.UUID(guid_text).bytes_le)
        buffer = ctypes.c_void_p()
        result = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(buffer))
        try:
            if result == 0 and buffer.value:
                path = ctypes.wstring_at(buffer.value)
                if path:
                    folders[name] = path
        finally:
            if buffer.value:
                ole32.CoTaskMemFree(buffer)
    return folders


@contextmanager
def background_priority(enabled: bool = True) -> Iterator[None]:
    """Run the enclosed block at background CPU and disk priority (Windows only)."""
    if not enabled or os.name != "nt":
        yield
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    process_mode_background_begin, process_mode_background_end = 0x00100000, 0x00200000
    process = kernel32.GetCurrentProcess()
    entered = bool(kernel32.SetPriorityClass(process, process_mode_background_begin))
    try:
        yield
    finally:
        if entered:
            kernel32.SetPriorityClass(process, process_mode_background_end)
