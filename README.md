# PortfolioForge

PortfolioForge is an agentic AI system that turns a scattered history of software engineering and machine learning work into a clean, published portfolio.

Past projects, especially academic ones, tend to be scattered: course folders on a laptop, `PycharmProjects`, `Downloads`, a personal Google Drive, and Google Colab notebooks. Often they have no README, no dependency file and no version control, and one course folder may hold several unrelated assignments. PortfolioForge aims to find these files, understand them, document them, and publish them to GitHub.

## Status

| Capability | Status |
|---|---|
| **Local file inventory** (targeted folders or the whole computer) | ✅ Implemented |
| Grouping files into projects (including across folders) | 🗓️ Planned |
| AI analysis of code, notebooks and documents | 🗓️ Planned |
| Google Drive and Google Colab sources | 🗓️ Planned |
| Documentation generation | 🗓️ Planned |
| Publishing to GitHub (after approval) | 🗓️ Planned |

Nothing in the current release uses an AI model, a cloud service, or the network.

## Local file inventory

The inventory is the first stage of the pipeline. It lists relevant files and records **facts only**: names, paths, folder structure, sizes and timestamps. It does not decide which files belong to which project. That's the job of the later grouping and AI stages.

### What it records

For each file:
- name and extension
- path relative to its scan root, the parent folder and the full folder chain
- absolute path
- category and language, inferred from the extension
- size, plus modified and created times
- the location it was found in, such as `User profile` or `Downloads`

It also writes a record for every folder visited, a record for everything skipped (with the reason), and summary statistics:
- totals, and counts by category, extension and language
- folders visited, skipped locations and errors
- cloud-only files that were skipped

Recognized formats include:

| Category | Examples |
|---|---|
| Code | `.py` `.java` `.js` `.ts` `.sql` `.r` `.rmd` `.c` `.cpp` `.cs` `.m` (MATLAB) `.jl` `.sas` `.do` `.html` `.tex` |
| Notebooks | `.ipynb` |
| Documents | `.pdf` `.docx` `.doc` `.odt` `.md` `.txt` |
| Presentations | `.pptx` `.ppt` `.odp` |
| Spreadsheets | `.xlsx` `.xls` `.ods` |
| Data | `.csv` `.tsv` `.json` `.parquet` `.mat` `.pkl` `.h5` `.npy` |
| Archives (listed, never opened) | `.zip` `.tar` `.gz` `.7z` `.rar` |
| Images (off by default) | `.png` `.jpg` `.svg` `.gif` |

### Safety guarantees

- **Read-only and metadata-only.**
  - Files are never opened, read, executed, modified or uploaded.
  - Only directory listings are used.
- **Local only.**
  - No network calls.
  - Results are written to `%LOCALAPPDATA%\PortfolioForge\inventories\`, outside the repository, and are never uploaded.
- **Never followed:** symbolic links and directory junctions.
- **Always excluded:**
  - Windows, system and application folders (`Windows`, `Program Files`, `ProgramData`, `$Recycle.Bin`, …)
  - `AppData`
  - credential folders (`.ssh`, `.aws`, `.gnupg`, …)
  - other accounts' profiles, unless you opt in
- **Credential-looking files** (`*.pem`, `*.key`, `id_rsa`, `credentials*.json`, `.env`, `secrets.*`, …) are skipped entirely. Their names are replaced with `[redacted]` in all output.
- **Excluded by default, configurable:**
  - hidden and system entries
  - virtual environments (detected by `pyvenv.cfg` or `conda-meta`, whatever the folder's name)
  - caches (`__pycache__`, `node_modules`, …) and build output (`build`, `out`, `dist`, …)
  - installed tools (`anaconda3`, folders containing an uninstaller or a Python installation)
  - installed R package libraries, and PortfolioForge's own source code (see [Filtering](#filtering) below)
  - OS metadata and Office lock files
- **No cloud downloads.**
  - Cloud-only placeholder files (Windows "Files On-Demand") are counted but never opened or inventoried.
  - Cloud-only folders are never entered.
  - Files already downloaded inside a folder named `OneDrive` are treated as ordinary local files. PortfolioForge does not connect to OneDrive.
- **Bounded work.** These limits are the defaults and can all be changed:
  - 1,000,000 files, 2,000,000 folders, folder depth 64 and a 30-minute time limit
  - scans run single-threaded at background priority
  - records are streamed to disk instead of held in memory
- **Incomplete scans are reported clearly.** If a limit is reached or a scan is cancelled, the results are kept, marked incomplete, and the reason is recorded.

### Filtering

The inventory leaves out installed software, so later stages see your work rather than libraries you installed. Every filtered folder is still recorded in `skipped.jsonl` with its reason, so nothing disappears silently.

**Installed R package libraries** (reason `r_package_library`) are recognized in two ways:

- **By folder name:**
  - `win-library`, the Windows per-user library
  - `site-library`, the Linux system library
  - `<platform>-library` inside a folder named `R`, such as `R/x86_64-pc-linux-gnu-library`
  - the project libraries `renv/library`, `renv/staging` and `renv/sandbox`
  - `packrat/lib`, `packrat/lib-R`, `packrat/lib-ext` and `packrat/src`
  - the `00LOCK*` folders that an interrupted install leaves behind
- **By layout, wherever the library lives:** any folder containing both a `DESCRIPTION` file and a `Meta` folder is an installed package. R creates `Meta` only when it installs a package. This covers macOS (`~/Library/R/.../library`) and custom library paths.

Your R work is **not** filtered:
- `.R` and `.Rmd` scripts are kept, as are course projects and their data
- folders that merely happen to be named `R`, `lib` or `library` are kept
- **R packages you wrote yourself** are kept, because their source (`DESCRIPTION`, `NAMESPACE`, `R/`, `man/`) has no `Meta` folder

**PortfolioForge's own source** (reason `portfolioforge_source`) is excluded so the tool doesn't inventory itself. Nothing about the location is hardcoded:
- **The copy you're running:** found from the location of PortfolioForge's own code. A regular `pip install` lives in `site-packages`, which is skipped anyway.
- **Other checkouts, such as forks or extra clones:** recognized by their layout, a `pyproject.toml` next to `src/portfolioforge/inventory`.
- **If you name a checkout explicitly in a targeted scan,** it's still scanned.

Both filters can be turned off from Python with `InventoryConfig(skip_r_libraries=False)` or `InventoryConfig(skip_portfolioforge_source=False)`. To exempt specific folder names, use `include_dir_names`. Files are classified only by their names and locations; their contents are never read.

### Running it

Scan specific folders (a targeted scan):

```powershell
python -m portfolioforge.inventory "C:\Users\me\PycharmProjects" "C:\Users\me\Documents\College"
```

Scan your files across the whole computer. This is opt-in: the scan plan is shown, and the scan only starts after you type `yes`:

```powershell
# Show what would be scanned, excluded and offered as optional, then exit
python -m portfolioforge.inventory --full-computer --plan-only

# Run it
python -m portfolioforge.inventory --full-computer
```

A full-computer scan covers:
- your entire user profile, including `Desktop`, `Documents`, `Downloads`, `PycharmProjects` and any other folders you created there
- known folders that Windows has moved elsewhere
- every non-system top-level folder on the system drive, such as `C:\CS101`

Other drives, other accounts' profiles and profiles left in `Windows.old` are listed in the plan as optional and are only scanned if you opt in:

```powershell
python -m portfolioforge.inventory --full-computer --include-drive D --include-location "C:\Windows.old\Users\me"
```

Other useful options:

| Option | What it does |
|---|---|
| `--include-images` | Also inventory image files |
| `--exclude PATH` | Skip a folder and everything below it (repeatable) |
| `--keep-build-dirs` | Don't skip `build`, `out` or `dist` folders |
| `--max-files` | Change the file limit |
| `--max-dirs` | Change the folder limit |
| `--max-depth` | Change the depth limit |
| `--time-limit MINUTES` | Change the time limit |
| `--output-dir PATH` | Write results somewhere else |

Run `--help` for the full list.

Results are written as `files.jsonl`, `directories.jsonl`, `skipped.jsonl`, `report.json` (metadata and summary) and `plan.json`.

### Using it from Python

```python
from portfolioforge.inventory import scan_inventory, load_inventory

inventory = scan_inventory(r"C:\Users\me\PycharmProjects")
for f in inventory.files:
    print(f.relative_path, f.category, f.language, f.modified_at)
print(inventory.summary.files_by_category)

saved = load_inventory(r"C:\Users\me\AppData\Local\PortfolioForge\inventories\20261101T120000Z")
```

Full-computer scans from Python also require explicit approval: `run_scan(plan_full_computer(), sink=..., allow_full_computer=True)`.

## Roadmap

1. **Discover**: inventory local files *(implemented)*, then Google Drive and Google Colab.
2. **Group**: work out which files belong together, including files spread across folders.
3. **Analyze**: work out what each project does, what technologies it uses, and how complete it is.
4. **Document**: generate a README, summary, setup instructions and highlights for each project.
5. **Publish**: prepare each project as a clean repository and, after you approve it, publish it to GitHub.

## Guiding principles

- **Human in the loop**: Nothing is published or changed on an external service without explicit approval.
- **Privacy first**: Secrets, credentials, personal data and large datasets are detected and excluded before anything leaves the machine.
- **Non-destructive**: Source files are read, never modified in place.

## Development

Requires Python 3.11+ (CI runs 3.13).

```powershell
pip install -e ".[dev]"
pytest -v
```

Tests run only against temporary folders and a simulated computer. They never scan your real files.

## Project layout

```
PortfolioForge/
├── README.md
├── pyproject.toml
├── .github/workflows/tests.yml     # CI: pytest on Python 3.13
├── src/portfolioforge/
│   └── inventory/
│       ├── formats.py              # extension → category/language registry
│       ├── rules.py                # exclusion and sensitive-file rules
│       ├── config.py               # scan settings and resource limits
│       ├── platform.py             # Windows drives, known folders, file attributes
│       ├── roots.py                # scan plans and automatic root discovery
│       ├── selfsource.py           # locates PortfolioForge's own source to exclude it
│       ├── scanner.py              # read-only, metadata-only walker
│       ├── models.py               # structured output (dataclasses)
│       ├── sinks.py                # in-memory and JSONL output
│       └── cli.py                  # python -m portfolioforge.inventory
└── tests/
```
