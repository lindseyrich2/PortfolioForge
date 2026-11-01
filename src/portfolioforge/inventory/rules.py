"""Built-in exclusion and sensitivity rules.

All name matching is case-insensitive: every name set below is lowercase and
is compared against ``name.casefold()``.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import AbstractSet, Iterable

#: Placeholder used instead of a sensitive filename in every exported result.
REDACTED = "[redacted]"

# --- Protected tier: always excluded, cannot be overridden -------------------

#: Windows system and application folders, matched directly below a drive root.
PROTECTED_TOP_LEVEL_DIRS: frozenset[str] = frozenset(
    {
        "windows",
        "windows.old",  # its Users\* profiles can be opted into explicitly
        "program files",
        "program files (x86)",
        "programdata",
        "$recycle.bin",
        "recycler",
        "system volume information",
        "recovery",
        "perflogs",
        "config.msi",
        "boot",
        "efi",
        "documents and settings",
        "msocache",
        "onedrivetemp",
        "intel",
        "amd",
        "nvidia",
        "$winreagent",
        "$sysreset",
        "$windows.~bt",
        "$windows.~ws",
        "$getcurrent",
    }
)

#: Private and credential locations, excluded wherever they appear.
CREDENTIAL_DIR_NAMES: frozenset[str] = frozenset(
    {"appdata", ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", "personal vault"}
)

#: Entries in a ``Users`` folder that are never user profiles.
NON_PROFILE_USER_DIRS: frozenset[str] = frozenset(
    {"default", "default user", "all users", "defaultapppool", "wdagutilityaccount"}
)

#: Filename patterns that look like credentials or private keys. Matching files
#: are skipped entirely and their names are redacted from results.
SENSITIVE_FILE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials*.json",
    "client_secret*.json",
    "service-account*.json",
    "service_account*.json",
    "token.json",
    "token-*.json",
    "token_*.json",
    "secrets.*",
    "secret.*",
    ".netrc",
    ".pgpass",
    ".htpasswd",
)

# --- Default tier: excluded by default, overridable via InventoryConfig -------

#: Cache, dependency and OS folders that never contain the user's own work.
JUNK_DIR_NAMES: frozenset[str] = frozenset(
    {
        "__pycache__",
        ".ipynb_checkpoints",
        "node_modules",
        "site-packages",
        "__macosx",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
    }
)

#: Folder names that are virtual environments by convention.
VIRTUAL_ENV_DIR_NAMES: frozenset[str] = frozenset({"venv", ".venv", "virtualenv"})

#: An entry with one of these names marks its folder as a virtual environment,
#: which catches environments with arbitrary names such as ``my_env``.
VIRTUAL_ENV_MARKERS: frozenset[str] = frozenset({"pyvenv.cfg", "conda-meta"})

#: Compiler and packaging output folders.
BUILD_DIR_NAMES: frozenset[str] = frozenset({"build", "dist", "target", "out", "bin", "obj"})
BUILD_DIR_SUFFIXES: tuple[str, ...] = (".egg-info",)

#: Installed tools, SDKs and game libraries, recognized by folder name.
INSTALLED_TOOL_DIR_NAMES: frozenset[str] = frozenset(
    {
        "anaconda",
        "anaconda3",
        "miniconda",
        "miniconda3",
        "scoop",
        "steamlibrary",
        "steamapps",
        "epic games",
        "xboxgames",
    }
)

#: A file with one of these names marks its folder as an installed application.
INSTALLED_TOOL_FILE_MARKERS: frozenset[str] = frozenset({"unins000.exe", "uninstall.exe", "node.exe"})

#: A folder holding all of these names is a Python installation.
PYTHON_INSTALL_MARKERS: frozenset[str] = frozenset({"python.exe", "lib"})

#: Installed R package libraries, recognized by folder name: the per-user
#: ``win-library`` on Windows, the system ``site-library`` on Linux, and
#: ``<platform>-library`` folders inside a folder named ``R`` (Linux user libraries).
R_LIBRARY_DIR_NAMES: frozenset[str] = frozenset({"win-library", "site-library"})
R_PLATFORM_LIBRARY_SUFFIX = "-library"
#: Project-local R package libraries: ``renv/library`` and friends, ``packrat/lib`` and friends.
R_PROJECT_LIBRARY_DIRS: dict[str, frozenset[str]] = {
    "renv": frozenset({"library", "staging", "sandbox"}),
    "packrat": frozenset({"lib", "lib-r", "lib-ext", "src"}),
}
#: A folder holding both of these is an *installed* R package. R creates ``Meta``
#: only when it installs a package, so a package's source (DESCRIPTION, NAMESPACE,
#: R/, man/) is not matched and is still inventoried.
R_INSTALLED_PACKAGE_MARKERS: frozenset[str] = frozenset({"description", "meta"})
#: Prefix of the temporary folders R leaves behind after an interrupted install.
R_INSTALL_LOCK_PREFIX = "00lock"

#: OS-generated metadata files.
OS_METADATA_FILES: frozenset[str] = frozenset({"thumbs.db", "ehthumbs.db", "desktop.ini", ".ds_store"})

#: Prefix of the temporary lock files Microsoft Office creates next to open documents.
OFFICE_LOCK_PREFIX = "~$"


# --- Project markers (inventory schema v2) ------------------------------------
#
# Names on these fixed lists are recorded on their folder's DirectoryRecord as
# evidence of a project root, even when the file itself is not inventoried
# (unrecognized extension, hidden) or the folder is skipped (.git, .idea).
# Only these exact names are ever recorded; other unrecognized files are counts.

PROJECT_MARKER_FILES: frozenset[str] = frozenset(
    {
        # documentation and licensing
        "readme", "readme.md", "readme.rst", "readme.txt",
        "license", "license.md", "license.txt", "copying",
        "contributing.md", "code_of_conduct.md", "changelog.md", "changes.md",
        # Python
        "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "pipfile",
        "pipfile.lock", "poetry.lock", "environment.yml", "environment.yaml", "tox.ini",
        # JavaScript / TypeScript
        "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "tsconfig.json",
        # JVM
        "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "gradlew", "build.xml",
        "manifest.mf", ".classpath", ".project",
        # C / C++ and others
        "makefile", "cmakelists.txt", "cargo.toml", "go.mod", "gemfile",
        # R
        "description", "namespace", "renv.lock", ".rprofile",
        # containers, notebooks and version control
        "dockerfile", "docker-compose.yml", "docker-compose.yaml", ".gitignore",
        ".gitattributes",
    }
)
PROJECT_MARKER_FILE_PATTERNS: tuple[str, ...] = ("*.rproj", "*.sln", "*.csproj", "*.vcxproj", "*.fsproj")
PROJECT_MARKER_DIRS: frozenset[str] = frozenset(
    {".git", ".github", ".gitlab", ".idea", ".vscode", ".settings", ".devcontainer",
     "nbproject", "renv", "packrat", ".ipynb_checkpoints"}
)


def is_project_marker_file(name: str) -> bool:
    folded = name.casefold()
    return folded in PROJECT_MARKER_FILES or any(
        fnmatchcase(folded, pattern) for pattern in PROJECT_MARKER_FILE_PATTERNS
    )


def is_project_marker_dir(name: str) -> bool:
    return name.casefold() in PROJECT_MARKER_DIRS


def is_sensitive_name(name: str, extra_patterns: Iterable[str] = ()) -> bool:
    folded = name.casefold()
    return any(
        fnmatchcase(folded, pattern.casefold())
        for pattern in (*SENSITIVE_FILE_PATTERNS, *extra_patterns)
    )


def is_virtual_env_listing(names: AbstractSet[str]) -> bool:
    """True if a folder's (casefolded) entry names mark it as a virtual environment."""
    return not VIRTUAL_ENV_MARKERS.isdisjoint(names)


def is_installed_tool_listing(names: AbstractSet[str]) -> bool:
    """True if a folder's (casefolded) entry names mark it as an installed tool."""
    return not INSTALLED_TOOL_FILE_MARKERS.isdisjoint(names) or PYTHON_INSTALL_MARKERS <= names


def is_build_dir_name(folded_name: str) -> bool:
    return folded_name in BUILD_DIR_NAMES or folded_name.endswith(BUILD_DIR_SUFFIXES)


def is_r_library_dir(folded_name: str, folded_parent: str) -> bool:
    """True if a folder's name (and its parent's name) mark it as an R package library."""
    if folded_name in R_LIBRARY_DIR_NAMES or folded_name.startswith(R_INSTALL_LOCK_PREFIX):
        return True
    if folded_parent == "r" and folded_name.endswith(R_PLATFORM_LIBRARY_SUFFIX):
        return True
    return folded_name in R_PROJECT_LIBRARY_DIRS.get(folded_parent, frozenset())


def is_installed_r_package_listing(names: AbstractSet[str]) -> bool:
    """True if a folder's (casefolded) entry names mark it as an installed R package."""
    return R_INSTALLED_PACKAGE_MARKERS <= names
