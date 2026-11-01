"""Filtering of installed R package libraries and of PortfolioForge's own source."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import file_paths, make_tree, skipped_pairs
from portfolioforge.inventory import InventoryConfig, plan_full_computer, plan_targeted, selfsource


def _installed_package(prefix: str, name: str) -> dict[str, str]:
    """Files R writes when it installs a package (DESCRIPTION + Meta/ + compiled R/)."""
    return {
        f"{prefix}/{name}/DESCRIPTION": "Package: x",
        f"{prefix}/{name}/NAMESPACE": "export(f)",
        f"{prefix}/{name}/Meta/package.rds": "",
        f"{prefix}/{name}/R/{name}.rdb": "",
        f"{prefix}/{name}/help/{name}.html": "<html/>",
        f"{prefix}/{name}/doc/intro.Rmd": "vignette",
        f"{prefix}/{name}/doc/intro.R": "code",
        f"{prefix}/{name}/include/{name}/header.hpp": "",
        f"{prefix}/{name}/tinytest/test_basic.R": "",
    }


# --- installed R libraries are skipped ------------------------------------------


def test_windows_win_library_is_skipped(scan, tmp_path):
    root = make_tree(
        tmp_path / "Documents",
        {
            **_installed_package("R/win-library/4.1", "ggplot2"),
            **_installed_package("R/win-library/4.1", "BH"),
            "R/my_analysis.R": "x <- 1",  # a folder named R is not a library
        },
    )
    inventory = scan(root)
    assert file_paths(inventory) == {"R/my_analysis.R"}
    assert ("R/win-library", "r_package_library") in skipped_pairs(inventory)


def test_linux_user_and_site_libraries_are_skipped(scan, tmp_path):
    root = make_tree(
        tmp_path / "home",
        {
            **_installed_package("R/x86_64-pc-linux-gnu-library/4.3", "dplyr"),
            **_installed_package("usr/lib/R/site-library", "MASS"),
        },
    )
    inventory = scan(root)
    assert file_paths(inventory) == set()
    assert {
        ("R/x86_64-pc-linux-gnu-library", "r_package_library"),
        ("usr/lib/R/site-library", "r_package_library"),
    } <= skipped_pairs(inventory)


def test_installed_packages_are_detected_by_structure_anywhere(scan, tmp_path):
    # e.g. macOS: ~/Library/R/arm64/4.3/library/<pkg>, or any custom library path
    root = make_tree(
        tmp_path / "home",
        {
            **_installed_package("Library/R/arm64/4.3/library", "tidyr"),
            **_installed_package("my-custom-rlib", "shiny"),
        },
    )
    inventory = scan(root)
    assert file_paths(inventory) == set()
    assert {
        ("Library/R/arm64/4.3/library/tidyr", "r_package_library"),
        ("my-custom-rlib/shiny", "r_package_library"),
    } <= skipped_pairs(inventory)


def test_renv_and_packrat_project_libraries_are_skipped_but_project_files_kept(scan, tmp_path):
    root = make_tree(
        tmp_path / "Statistics Course",
        {
            **_installed_package("RenvProject/renv/library/R-4.1/x86_64-w64-mingw32", "dplyr"),
            "RenvProject/renv/staging/1/pkg.R": "",
            "RenvProject/analysis.Rmd": "report",
            "RenvProject/renv.lock": "{}",
            **_installed_package("PackratProject/packrat/lib/x86_64-w64-mingw32/4.1.2", "ggplot2"),
            "PackratProject/packrat/src/ggplot2/ggplot2_3.4.0.tar.gz": "",
            "PackratProject/model.R": "fit <- lm(y ~ x)",
        },
    )
    inventory = scan(root)
    assert file_paths(inventory) == {"RenvProject/analysis.Rmd", "PackratProject/model.R"}
    assert {
        ("RenvProject/renv/library", "r_package_library"),
        ("RenvProject/renv/staging", "r_package_library"),
        ("PackratProject/packrat/lib", "r_package_library"),
        ("PackratProject/packrat/src", "r_package_library"),
    } <= skipped_pairs(inventory)


def test_interrupted_install_lock_folders_are_skipped(scan, tmp_path):
    root = make_tree(tmp_path / "lib", {"00LOCK-sf/sf/R/sf.R": "", "keep.R": ""})
    inventory = scan(root)
    assert file_paths(inventory) == {"keep.R"}
    assert ("00LOCK-sf", "r_package_library") in skipped_pairs(inventory)


# --- genuine R coursework is kept -----------------------------------------------


def test_r_coursework_is_inventoried(scan, tmp_path):
    root = make_tree(
        tmp_path / "Statistics Course",
        {
            "Regression Study/code/models/model.R": "",
            "Regression Study/code/features.R": "",
            "Regression Study/setup/libs.R": "library(data.table)",
            "Regression Study/data/raw/train.csv": "a,b",
            "Exam Review/analysis.Rmd": "",
            "Exam Review - Copy/analysis.Rmd": "",
            "R/hw1.R": "",  # folder named R
            "library/notes.txt": "",  # folder named library, not under renv
            "lib/helper.R": "",  # folder named lib, not under packrat
        },
    )
    assert file_paths(scan(root)) == {
        "Regression Study/code/models/model.R",
        "Regression Study/code/features.R",
        "Regression Study/setup/libs.R",
        "Regression Study/data/raw/train.csv",
        "Exam Review/analysis.Rmd",
        "Exam Review - Copy/analysis.Rmd",
        "R/hw1.R",
        "library/notes.txt",
        "lib/helper.R",
    }


def test_users_own_r_package_source_is_inventoried(scan, tmp_path):
    # Package *source* has DESCRIPTION and NAMESPACE but no Meta/ folder.
    root = make_tree(
        tmp_path / "Projects",
        {
            "mypkg/DESCRIPTION": "Package: mypkg",
            "mypkg/NAMESPACE": "export(f)",
            "mypkg/R/functions.R": "f <- function() 1",
            "mypkg/tests/testthat/test-f.R": "",
            "mypkg/vignettes/intro.Rmd": "",
        },
    )
    assert file_paths(scan(root)) == {
        "mypkg/R/functions.R",
        "mypkg/tests/testthat/test-f.R",
        "mypkg/vignettes/intro.Rmd",
    }


def test_r_library_filtering_can_be_turned_off_or_overridden(scan, tmp_path):
    root = make_tree(tmp_path / "Documents", _installed_package("R/win-library/4.1", "ggplot2"))
    everything = scan(root, config=InventoryConfig(skip_r_libraries=False))
    assert "R/win-library/4.1/ggplot2/doc/intro.R" in file_paths(everything)

    by_name = scan(root, config=InventoryConfig(include_dir_names=frozenset({"win-library"})))
    # the named library folder is entered, but each installed package is still recognized
    assert ("R/win-library/4.1/ggplot2", "r_package_library") in skipped_pairs(by_name)


# --- PortfolioForge's own source ------------------------------------------------


def test_running_checkout_is_located_portably():
    repo_root = Path(__file__).resolve().parents[1]
    located = selfsource.portfolioforge_source_dir()
    if located is None:
        pytest.skip("PortfolioForge is not running from a source checkout")
    assert Path(located) == repo_root
    assert (Path(located) / "pyproject.toml").is_file()


def test_running_checkout_is_skipped(scan, tmp_path, monkeypatch):
    projects = make_tree(
        tmp_path / "Projects",
        {"PortfolioForge/README.md": "", "PortfolioForge/anything.py": "", "Coursework/hw.py": ""},
    )
    monkeypatch.setattr(selfsource, "portfolioforge_source_dir", lambda: str(projects / "PortfolioForge"))
    inventory = scan(projects)
    assert file_paths(inventory) == {"Coursework/hw.py"}
    assert ("PortfolioForge", "portfolioforge_source") in skipped_pairs(inventory)

    kept = scan(projects, config=InventoryConfig(skip_portfolioforge_source=False))
    assert "PortfolioForge/anything.py" in file_paths(kept)


def test_other_checkouts_are_recognized_by_layout(scan, tmp_path):
    root = make_tree(
        tmp_path / "Clones",
        {
            "PortfolioForge-fork/pyproject.toml": "",
            "PortfolioForge-fork/src/portfolioforge/inventory/scanner.py": "",
            "OtherTool/pyproject.toml": "",
            "OtherTool/src/othertool/main.py": "",
        },
    )
    inventory = scan(root)
    assert file_paths(inventory) == {"OtherTool/src/othertool/main.py"}
    assert ("PortfolioForge-fork", "portfolioforge_source") in skipped_pairs(inventory)


def test_explicitly_targeted_checkout_is_still_scanned(fake_system, tmp_path, monkeypatch):
    checkout = make_tree(tmp_path / "PortfolioForge", {"pyproject.toml": "", "src/portfolioforge/inventory/x.py": ""})
    monkeypatch.setattr(selfsource, "portfolioforge_source_dir", lambda: str(checkout))
    plan = plan_targeted([checkout], system=fake_system)
    assert [r.path for r in plan.roots] == [str(checkout)]
    assert not any(e.reason.value == "portfolioforge_source" for e in plan.excluded)


def test_full_plan_shows_and_never_roots_the_checkout(fake_computer, monkeypatch):
    fc = fake_computer
    inside_profile = fc.profile / "Projects" / "PortfolioForge"
    inside_profile.mkdir(parents=True)
    monkeypatch.setattr(selfsource, "portfolioforge_source_dir", lambda: str(inside_profile))
    plan = plan_full_computer(system=fc.system)
    assert (str(inside_profile), "portfolioforge_source") in {(e.path, e.reason.value) for e in plan.excluded}

    top_level = fc.c / "PortfolioForge"
    top_level.mkdir()
    monkeypatch.setattr(selfsource, "portfolioforge_source_dir", lambda: str(top_level))
    plan = plan_full_computer(system=fc.system)
    assert str(top_level) not in {r.path for r in plan.roots}
    assert (str(top_level), "portfolioforge_source") in {(e.path, e.reason.value) for e in plan.excluded}
