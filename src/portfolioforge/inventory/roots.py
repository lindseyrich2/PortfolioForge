"""Scan plans: which folders to scan, which are excluded, and which are optional.

Building a plan only lists the top level of the system drive and the profile
folders. It never walks a tree, opens a file, or touches the network.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Iterable

from . import rules, selfsource
from .config import FullScanOptions
from .models import (
    OptionalLocation,
    PlanExclusion,
    RootOrigin,
    ScanMode,
    ScanPlan,
    ScanRoot,
    SkipReason,
)
from .platform import (
    FILE_ATTRIBUTE_HIDDEN,
    FILE_ATTRIBUTE_SYSTEM,
    DriveInfo,
    DriveType,
    SystemInfo,
    detect_system,
    file_attributes,
    is_link_path,
    is_link_stat,
)


class ProtectedLocationError(ValueError):
    """Raised when asked to scan a protected, credential or link location."""


# --- path helpers --------------------------------------------------------------


def normalize_path(path: str | os.PathLike[str]) -> str:
    """Absolute, normalized, case-normalized (on Windows) form for comparisons."""
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def is_within(child: str, parent: str) -> bool:
    """True if normalized ``child`` equals or is inside normalized ``parent``."""
    if child == parent:
        return True
    prefix = parent if parent.endswith(("\\", "/")) else parent + os.sep
    return child.startswith(prefix)


def _relative_parts(child: str, parent: str) -> tuple[str, ...] | None:
    if not is_within(child, parent):
        return None
    rest = child[len(parent):]
    return tuple(part for part in re.split(r"[\\/]+", rest) if part)


def root_id_for(path: str) -> str:
    digest = hashlib.sha256(normalize_path(path).encode("utf-8", "surrogatepass"))
    return "r_" + digest.hexdigest()[:12]


def _make_root(path: str, origin: RootOrigin, label: str) -> ScanRoot:
    return ScanRoot(id=root_id_for(path), path=path, origin=origin, label=label)


def _users_dirs(system: SystemInfo) -> tuple[str, ...]:
    return (os.path.dirname(os.path.abspath(system.home)),) if system.home else ()


def _drive_roots(system: SystemInfo) -> tuple[str, ...]:
    candidates = [system.system_drive, *(d.root for d in system.drives)]
    return tuple(dict.fromkeys(c for c in candidates if c))


# --- protection ----------------------------------------------------------------


def protected_reason(
    path: str,
    *,
    drive_roots: Iterable[str] = (),
    users_dirs: Iterable[str] = (),
) -> SkipReason | None:
    """Why ``path`` may never be used as a scan root, or None if it may.

    Checks system/application folders below each drive root (and the path's own
    drive), and credential locations such as ``AppData`` or ``.ssh`` inside any
    user profile.
    """
    norm = normalize_path(path)
    bases = {normalize_path(d) for d in drive_roots}
    anchor = Path(norm).anchor
    if anchor:
        bases.add(normalize_path(anchor))
    for base in bases:
        rel = _relative_parts(norm, base)
        if not rel:
            continue
        top = rel[0].casefold()
        if top == "windows.old" and len(rel) >= 3 and rel[1].casefold() == "users":
            if any(p.casefold() in rules.CREDENTIAL_DIR_NAMES for p in rel[3:]):
                return SkipReason.CREDENTIAL_LOCATION
            continue  # an old profile can be opted into explicitly
        if top in rules.PROTECTED_TOP_LEVEL_DIRS:
            return SkipReason.PROTECTED_LOCATION
    for users in users_dirs:
        rel = _relative_parts(norm, normalize_path(users))
        if rel and any(p.casefold() in rules.CREDENTIAL_DIR_NAMES for p in rel[1:]):
            return SkipReason.CREDENTIAL_LOCATION
    return None


def _validate_root(path: str, system: SystemInfo, output_dir: str | None) -> str:
    path = os.path.abspath(os.fspath(path))
    if not os.path.exists(path):
        raise FileNotFoundError(f"Folder not found: {path}")
    if not os.path.isdir(path):
        raise NotADirectoryError(f"Not a folder: {path}")
    if is_link_path(path):
        raise ProtectedLocationError(
            f"Refusing to scan a link or junction: {path}. Scan the folder it points to instead."
        )
    reason = protected_reason(path, drive_roots=_drive_roots(system), users_dirs=_users_dirs(system))
    if reason is not None:
        raise ProtectedLocationError(f"Refusing to scan a protected location ({reason.value}): {path}")
    if output_dir and is_within(normalize_path(path), normalize_path(output_dir)):
        raise ProtectedLocationError(f"Refusing to scan inside the inventory output folder: {path}")
    return path


def _dedupe(roots: list[ScanRoot]) -> tuple[tuple[ScanRoot, ...], list[PlanExclusion]]:
    """Drop roots that duplicate or sit inside another root."""
    norms = [normalize_path(r.path) for r in roots]
    order = sorted(range(len(roots)), key=lambda i: (len(norms[i]), i))
    kept: list[int] = []
    dropped: list[PlanExclusion] = []
    for i in order:
        if any(is_within(norms[i], norms[k]) for k in kept):
            dropped.append(PlanExclusion(roots[i].path, SkipReason.OVERLAPPING_ROOT))
        else:
            kept.append(i)
    return tuple(roots[i] for i in sorted(kept)), dropped


def _plan(
    mode: ScanMode,
    roots: list[ScanRoot],
    excluded: list[PlanExclusion],
    optional: list[OptionalLocation],
    system: SystemInfo,
    output_dir: str | os.PathLike[str] | None,
    exclude_paths: Iterable[str | os.PathLike[str]] = (),
    drop_self_source_roots: bool = False,
) -> ScanPlan:
    user_excluded = [os.path.abspath(os.fspath(p)) for p in exclude_paths]
    excluded = [*excluded, *(PlanExclusion(p, SkipReason.USER_EXCLUDED) for p in user_excluded)]
    excluded_norms = [normalize_path(p) for p in user_excluded]
    roots = [
        r for r in roots
        if not any(is_within(normalize_path(r.path), ex) for ex in excluded_norms)
    ]
    # PortfolioForge's own checkout: discovered roots inside it are dropped, and
    # the plan notes it whenever the walk would reach (and skip) it. A checkout
    # the user explicitly targets is still scanned.
    source = selfsource.portfolioforge_source_dir()
    source_norm = normalize_path(source) if source else None
    note_source = False
    if source_norm and drop_self_source_roots:
        remaining = [r for r in roots if not is_within(normalize_path(r.path), source_norm)]
        note_source = len(remaining) != len(roots)
        roots = remaining
    if not roots:
        raise ValueError("Every scan root is excluded; nothing would be scanned.")
    kept, overlapping = _dedupe(roots)
    if source_norm and not note_source:
        note_source = any(
            is_within(source_norm, normalize_path(r.path)) and source_norm != normalize_path(r.path)
            for r in kept
        )
    if note_source:
        excluded = [*excluded, PlanExclusion(source, SkipReason.PORTFOLIOFORGE_SOURCE)]
    out = os.path.abspath(os.fspath(output_dir)) if output_dir else None
    if out:
        excluded = [*excluded, PlanExclusion(out, SkipReason.OUTPUT_DIR)]
    return ScanPlan(
        mode=mode,
        roots=kept,
        excluded=tuple([*excluded, *overlapping]),
        optional=tuple(optional),
        profile_dir=os.path.abspath(system.home) if system.home else None,
        users_dirs=_users_dirs(system),
        drive_roots=_drive_roots(system),
        output_dir=out,
    )


# --- targeted plans --------------------------------------------------------------


def plan_targeted(
    paths: Iterable[str | os.PathLike[str]],
    *,
    system: SystemInfo | None = None,
    output_dir: str | os.PathLike[str] | None = None,
    exclude_paths: Iterable[str | os.PathLike[str]] = (),
) -> ScanPlan:
    """Plan a scan of explicitly selected folders.

    Roots inside ``exclude_paths`` are dropped; pass the same paths to
    ``InventoryConfig.exclude_paths`` to also skip them inside other roots.
    """
    system = system or detect_system()
    out = os.fspath(output_dir) if output_dir else None
    roots = []
    for raw in paths:
        path = _validate_root(os.fspath(raw), system, out)
        roots.append(_make_root(path, RootOrigin.USER_SELECTED, path))
    if not roots:
        raise ValueError("A targeted scan needs at least one folder.")
    return _plan(ScanMode.TARGETED, roots, [], [], system, output_dir, exclude_paths)


# --- full-computer plans ---------------------------------------------------------


def plan_full_computer(
    options: FullScanOptions | None = None,
    *,
    system: SystemInfo | None = None,
    output_dir: str | os.PathLike[str] | None = None,
    exclude_paths: Iterable[str | os.PathLike[str]] = (),
) -> ScanPlan:
    """Plan a scan of the user's files across this computer.

    Roots: the whole user profile; known folders (Desktop, Documents, ...)
    located outside it; and every top-level folder on the system drive that is
    not a system, application or installed-tool folder. Other drives, other
    accounts' profiles and profiles in ``Windows.old`` are listed as optional
    locations and only become roots when named in ``options``.
    """
    options = options or FullScanOptions()
    system = system or detect_system()
    home = os.path.abspath(system.home)
    roots: list[ScanRoot] = [_make_root(home, RootOrigin.PROFILE, "User profile")]
    excluded: list[PlanExclusion] = []
    optional: list[OptionalLocation] = []
    guard = {"drive_roots": _drive_roots(system), "users_dirs": _users_dirs(system)}

    for name, folder in sorted(system.known_folders.items()):
        if not folder or not os.path.isdir(folder):
            continue
        if is_link_path(folder):
            excluded.append(PlanExclusion(folder, SkipReason.LINK))
        elif (reason := protected_reason(folder, **guard)) is not None:
            excluded.append(PlanExclusion(folder, reason))
        else:
            roots.append(_make_root(os.path.abspath(folder), RootOrigin.KNOWN_FOLDER, name))

    if system.system_drive and os.path.isdir(system.system_drive):
        _survey_drive_top_level(system.system_drive, home, roots, excluded, optional)

    requested = {_drive_letter(spec) for spec in options.include_drives}
    seen_letters: set[str] = set()
    system_norm = normalize_path(system.system_drive) if system.system_drive else None
    for drive in system.drives:
        letter = drive.letter.upper()
        seen_letters.add(letter)
        if system_norm and normalize_path(drive.root) == system_norm:
            continue
        includable, why_not = _drive_includable(drive)
        if letter in requested:
            if not includable:
                raise ProtectedLocationError(f"Drive {letter}: cannot be scanned: {why_not}")
            roots.append(_make_root(drive.root, RootOrigin.ADDITIONAL_DRIVE, f"Drive {letter}:"))
        else:
            description = _describe_drive(drive) + (f" ({why_not})" if why_not else "")
            optional.append(
                OptionalLocation(
                    path=drive.root,
                    kind="additional_drive",
                    description=description,
                    includable=includable,
                    include_hint=f"--include-drive {letter}" if includable else None,
                )
            )
    if unknown := requested - seen_letters:
        raise ValueError(f"Drive not found: {', '.join(sorted(unknown))}")

    optional_by_path = {normalize_path(o.path): o for o in optional if o.kind != "additional_drive"}
    for raw in options.include_locations:
        path = _validate_root(os.fspath(raw), system, os.fspath(output_dir) if output_dir else None)
        match = optional_by_path.pop(normalize_path(path), None)
        if match is not None:
            optional.remove(match)
            roots.append(_make_root(path, RootOrigin.OTHER_PROFILE, match.description))
        else:
            roots.append(_make_root(path, RootOrigin.USER_SELECTED, path))

    return _plan(
        ScanMode.FULL_COMPUTER, roots, excluded, optional, system, output_dir, exclude_paths,
        drop_self_source_roots=True,
    )


def _survey_drive_top_level(
    drive_root: str,
    home: str,
    roots: list[ScanRoot],
    excluded: list[PlanExclusion],
    optional: list[OptionalLocation],
) -> None:
    home_norm = normalize_path(home)
    try:
        with os.scandir(drive_root) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError as exc:
        excluded.append(PlanExclusion(drive_root, _error_reason(exc)))
        return
    for entry in entries:
        path = os.path.join(drive_root, entry.name)
        folded = entry.name.casefold()
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError as exc:
            excluded.append(PlanExclusion(path, _error_reason(exc)))
            continue
        if is_link_stat(st):
            excluded.append(PlanExclusion(path, SkipReason.LINK))
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue
        if folded == "users":
            _survey_profiles(path, home_norm, "other_profile", "Another account's profile", optional)
            continue
        if folded == "windows.old":
            excluded.append(PlanExclusion(path, SkipReason.PROTECTED_LOCATION))
            _survey_profiles(
                os.path.join(path, "Users"), home_norm, "old_windows_profile",
                "Profile from a previous Windows installation", optional,
            )
            continue
        if folded in rules.PROTECTED_TOP_LEVEL_DIRS:
            excluded.append(PlanExclusion(path, SkipReason.PROTECTED_LOCATION))
            continue
        attrs = file_attributes(st)
        if entry.name.startswith(".") or attrs & FILE_ATTRIBUTE_HIDDEN:
            excluded.append(PlanExclusion(path, SkipReason.HIDDEN))
            continue
        if attrs & FILE_ATTRIBUTE_SYSTEM:
            excluded.append(PlanExclusion(path, SkipReason.SYSTEM))
            continue
        if folded in rules.INSTALLED_TOOL_DIR_NAMES:
            excluded.append(PlanExclusion(path, SkipReason.INSTALLED_TOOL))
            continue
        names = _listing_names(path)
        if names is not None and rules.is_virtual_env_listing(names):
            excluded.append(PlanExclusion(path, SkipReason.VIRTUAL_ENV))
        elif names is not None and rules.is_installed_tool_listing(names):
            excluded.append(PlanExclusion(path, SkipReason.INSTALLED_TOOL))
        elif normalize_path(path) != home_norm:
            roots.append(_make_root(path, RootOrigin.DRIVE_TOP_LEVEL, path))


def _survey_profiles(
    users_dir: str,
    home_norm: str,
    kind: str,
    description: str,
    optional: list[OptionalLocation],
) -> None:
    try:
        with os.scandir(users_dir) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_symlink() or is_link_stat(entry.stat(follow_symlinks=False)):
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        if entry.name.casefold() in rules.NON_PROFILE_USER_DIRS:
            continue
        path = os.path.join(users_dir, entry.name)
        if normalize_path(path) == home_norm:
            continue
        optional.append(
            OptionalLocation(
                path=path,
                kind=kind,
                description=f"{description}: {entry.name}",
                includable=True,
                include_hint=f'--include-location "{path}"',
            )
        )


def _listing_names(path: str) -> set[str] | None:
    try:
        with os.scandir(path) as it:
            return {e.name.casefold() for e in it}
    except OSError:
        return None


def _drive_letter(spec: str) -> str:
    letter = spec.strip().rstrip("\\/").rstrip(":").upper()
    if len(letter) != 1 or not letter.isalpha():
        raise ValueError(f"Not a drive letter: {spec!r}")
    return letter


def _is_virtual_cloud_drive(drive: DriveInfo) -> bool:
    label = (drive.label or "").casefold()
    return "google drive" in label or "onedrive" in label


def _drive_includable(drive: DriveInfo) -> tuple[bool, str | None]:
    if drive.drive_type in (DriveType.NETWORK, DriveType.OPTICAL, DriveType.UNKNOWN):
        return False, f"{drive.drive_type.value} drives are not scanned"
    if _is_virtual_cloud_drive(drive):
        return False, "virtual cloud drive; cloud sources are not scanned from the local disk"
    return True, None


def _describe_drive(drive: DriveInfo) -> str:
    parts = [f"{drive.drive_type.value} drive"]
    if drive.label:
        parts.append(f'"{drive.label}"')
    if drive.filesystem:
        parts.append(drive.filesystem)
    return " ".join(parts)


def _error_reason(exc: OSError) -> SkipReason:
    return SkipReason.PERMISSION_ERROR if isinstance(exc, PermissionError) else SkipReason.OS_ERROR
