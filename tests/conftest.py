"""Shared fixtures. Every test works inside pytest's temporary directories.

No test scans the real computer: full-computer tests describe a fake computer
with a hand-built ``SystemInfo`` whose drives and profile live under tmp_path.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from portfolioforge.inventory import DriveInfo, DriveType, Inventory, SystemInfo, scan_inventory


def make_tree(base: Path, files: dict[str, str], dirs: tuple[str, ...] = ()) -> Path:
    for relative, content in files.items():
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for relative in dirs:
        (base / relative).mkdir(parents=True, exist_ok=True)
    return base


def file_paths(inventory: Inventory) -> set[str]:
    return {f.relative_path for f in inventory.files}


def skipped_pairs(inventory: Inventory) -> set[tuple[str, str]]:
    return {(s.relative_path, s.reason.value) for s in inventory.skipped}


# A realistic college folder: several assignments per course folder, generic
# notebook names, no READMEs or project config files, plus the clutter a real
# folder accumulates (environments, caches, lock files, credentials).
ACADEMIC_FILES: dict[str, str] = {
    "CS101 Intro/hw1.py": "print('hello')\n",
    "CS101 Intro/hw2.py": "x = 2\n",
    "CS101 Intro/hw3/main.py": "def main(): pass\n",
    "CS101 Intro/hw3/input.txt": "1 2 3\n",
    "CS101 Intro/Lab Reports/Lab 1.docx": "docx",
    "CS101 Intro/Lab Reports/~$Lab 1.docx": "lock",
    "CS101 Intro/__pycache__/hw1.cpython-313.pyc": "pyc",
    "CS229 ML/Untitled.ipynb": "{}",
    "CS229 ML/Untitled1.ipynb": "{}",
    "CS229 ML/Assignment 2/Untitled.ipynb": "{}",
    "CS229 ML/.ipynb_checkpoints/Untitled-checkpoint.ipynb": "{}",
    "CS229 ML/final_project/model.ipynb": "{}",
    "CS229 ML/final_project/data/train.csv": "a,b\n1,2\n",
    "CS229 ML/final_project/Final Report.pdf": "pdf",
    "CS229 ML/final_project/slides.pptx": "pptx",
    "CS229 ML/final_project/figures/loss_curve.png": "png",
    "CS229 ML/final_project/credentials.json": '{"secret": true}',
    "CS229 ML/final_project/.env": "API_KEY=abc",
    "Databases/queries.sql": "SELECT 1;",
    "Databases/schema.SQL": "CREATE TABLE t (id int);",
    "Databases/grades.xlsx": "xlsx",
    "Stats/analysis.R": "x <- 1",
    "Stats/helper.r": "y <- 2",
    "Stats/数据分析.csv": "a\n1\n",
    "Stats/Résumé notes.txt": "notes",
    "Java Course/src/Main.java": "class Main {}",
    "Java Course/out/run_output.txt": "output",
    "Java Course/bin/Main.class": "class",
    "Java Course/.idea/workspace.xml": "<xml/>",
    "RL Project/env/gridworld.py": "class GridWorld: pass\n",
    "Archive/old_hw.zip": "zip",
    "Misc/notes.xyz": "?",
    "Misc/Makefile": "all:",
    "keys/id_rsa": "PRIVATE KEY",
    "keys/server.pem": "PRIVATE KEY",
    "notebook.ipynb": "{}",
    "venv/pyvenv.cfg": "home = x",
    "venv/Lib/site-packages/pkg.py": "",
    "my_env/pyvenv.cfg": "home = x",
    "my_env/Lib/thing.py": "",
    "conda_env/conda-meta/history": "",
    "conda_env/lib/other.py": "",
    "node_modules/lodash/index.js": "",
    "__MACOSX/CS229 ML/._Untitled.ipynb": "",
    "Thumbs.db": "",
    "desktop.ini": "",
}
ACADEMIC_DIRS = ("Empty Folder",)

EXPECTED_ACADEMIC_FILES = {
    "CS101 Intro/hw1.py",
    "CS101 Intro/hw2.py",
    "CS101 Intro/hw3/main.py",
    "CS101 Intro/hw3/input.txt",
    "CS101 Intro/Lab Reports/Lab 1.docx",
    "CS229 ML/Untitled.ipynb",
    "CS229 ML/Untitled1.ipynb",
    "CS229 ML/Assignment 2/Untitled.ipynb",
    "CS229 ML/final_project/model.ipynb",
    "CS229 ML/final_project/data/train.csv",
    "CS229 ML/final_project/Final Report.pdf",
    "CS229 ML/final_project/slides.pptx",
    "Databases/queries.sql",
    "Databases/schema.SQL",
    "Databases/grades.xlsx",
    "Stats/analysis.R",
    "Stats/helper.r",
    "Stats/数据分析.csv",
    "Stats/Résumé notes.txt",
    "Java Course/src/Main.java",
    "RL Project/env/gridworld.py",
    "Archive/old_hw.zip",
    "notebook.ipynb",
}


@pytest.fixture
def fake_system(tmp_path: Path) -> SystemInfo:
    """A minimal system whose profile does not overlap the test trees."""
    home = tmp_path / "fakehost" / "Users" / "student"
    home.mkdir(parents=True)
    return SystemInfo(home=str(home))


@pytest.fixture
def college(tmp_path: Path) -> Path:
    return make_tree(tmp_path / "College", ACADEMIC_FILES, ACADEMIC_DIRS)


@pytest.fixture
def scan(fake_system: SystemInfo):
    def _scan(*paths: Path, **kwargs) -> Inventory:
        return scan_inventory(paths, system=fake_system, **kwargs)

    return _scan


@pytest.fixture
def fake_computer(tmp_path: Path) -> SimpleNamespace:
    """A fake Windows computer: system drive C, data drive D, and more."""
    base = tmp_path / "computer"
    make_tree(
        base,
        {
            "C/Windows/System32/driver.py": "",
            "C/Program Files/App/plugin.py": "",
            "C/ProgramData/App/settings.json": "",
            "C/$Recycle.Bin/S-1-5-21/deleted.py": "",
            "C/CS101/hw1.py": "",
            "C/Python39/python.exe": "",
            "C/Python39/Lib/os.py": "",
            "C/Users/student/PycharmProjects/tictactoe/main.py": "",
            "C/Users/student/Documents/Essay.docx": "",
            "C/Users/student/Downloads/Untitled.ipynb": "",
            "C/Users/student/Desktop/Lab3.java": "",
            "C/Users/student/OneDrive/School/Stats/analysis.R": "",
            "C/Users/student/AppData/Roaming/App/script.py": "",
            "C/Users/student/.ssh/config": "",
            "C/Users/student/anaconda3/Lib/site.py": "",
            "C/Users/OldAccount/Documents/project.py": "",
            "C/Users/Public/Documents/shared.csv": "",
            "C/Windows.old/Users/student/Documents/old_hw.py": "",
            "D/School/thesis.ipynb": "",
            "D/Program Files/Game/game.py": "",
            "RedirectedDocs/notes.txt": "",
        },
        dirs=("C/Users/Default", "E", "G", "N"),
    )
    c, d = base / "C", base / "D"
    profile = c / "Users" / "student"
    system = SystemInfo(
        home=str(profile),
        system_drive=str(c),
        drives=(
            DriveInfo("C", str(c), DriveType.FIXED, "OS", "NTFS"),
            DriveInfo("D", str(d), DriveType.FIXED, "Data", "NTFS"),
            DriveInfo("E", str(base / "E"), DriveType.REMOVABLE),
            DriveInfo("G", str(base / "G"), DriveType.FIXED, "Google Drive", "FAT32"),
            DriveInfo("N", str(base / "N"), DriveType.NETWORK),
        ),
        known_folders={
            "Desktop": str(profile / "Desktop"),
            "Documents": str(base / "RedirectedDocs"),
            "Downloads": str(profile / "Downloads"),
        },
    )
    return SimpleNamespace(base=base, c=c, d=d, profile=profile, system=system)


def rel_to(base: Path, absolute: str) -> str:
    return Path(absolute).relative_to(base).as_posix()
