# PortfolioForge

PortfolioForge is an agentic AI system that turns a scattered history of software engineering and machine learning work into a clean, published portfolio.

Past projects, especially academic ones, tend to be scattered: course folders on a laptop, `PycharmProjects`, `Downloads`, a personal Google Drive, and Google Colab notebooks. Often they have no README, no dependency file and no version control, and one course folder may hold several unrelated assignments. PortfolioForge aims to find these files, understand them, document them, and publish them to GitHub.

## Status

| Capability | Status |
|---|---|
| **Local file inventory** (targeted folders or the whole computer) | ✅ Implemented |
| **Privacy safeguards and local data management** | ✅ Implemented |
| **Candidate grouping** of files into courses, assignments and projects (metadata only, reviewed by you) | ✅ Implemented |
| Project understanding and descriptive naming (AI, approval-gated; [design](docs/design/project-understanding-and-naming.md)) | 🗓️ Planned |
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

It also writes a record for every folder visited, a record for everything skipped (with the reason), and summary statistics.

Each folder record (inventory format version 2) also lists:
- **Project-marker names** found in that folder: `README.md`, `requirements.txt`, `pyproject.toml`, `DESCRIPTION`, `Makefile`, `*.Rproj`, and marker folders such as `.git`, `.idea` and `nbproject`. Only names on a **fixed list** are recorded, even when the file itself isn't inventoried.
- **Counts of unrecognized extensions.** Other files with unrecognized extensions are only counted per folder; their names are never recorded.

Version 1 inventories still load.

The summary statistics cover:
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
  - No network calls and no telemetry.
  - Results are written to your own data folder (see [Privacy and your data](#privacy-and-your-data)) and are never uploaded.
  - PortfolioForge refuses to write its data inside any Git repository.
- **Never followed:** symbolic links and directory junctions.
- **Always excluded:**
  - Windows, system and application folders (`Windows`, `Program Files`, `ProgramData`, `$Recycle.Bin`, …)
  - `AppData`
  - credential folders (`.ssh`, `.aws`, `.gnupg`, …)
  - **private folders**: never even listed, with their names replaced by `[private]` (see [Private folders](#private-folders))
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
| `--output-dir PATH` | Write results somewhere else (never inside a Git repository) |
| `--redact` | Show no paths or file or folder names anywhere in the output, including the plan, the summary and error messages, so the output is safe to share |
| `--privacy-config FILE` | Use this privacy settings file instead of the default |

Run `--help` for the full list.

Results are written as `files.jsonl`, `directories.jsonl`, `skipped.jsonl`, `report.json` (metadata and summary) and `plan.json`. To list, summarize or delete saved results, see [Managing your data](#managing-your-data).

### Using it from Python

```python
from portfolioforge.inventory import InventoryConfig, load_inventory, scan_inventory
from portfolioforge.paths import inventories_dir
from portfolioforge.privacy import load_privacy_settings

config = InventoryConfig().with_privacy(load_privacy_settings())
inventory = scan_inventory(r"C:\Users\me\PycharmProjects", config=config)
for f in inventory.files:
    print(f.relative_path, f.category, f.language, f.modified_at)
print(inventory.summary.files_by_category)

saved = load_inventory(inventories_dir() / "20261101T120000Z")
```

Full-computer scans from Python also require explicit approval: `run_scan(plan_full_computer(), sink=..., allow_full_computer=True)`.

## Candidate grouping

The grouping stage reads a **saved inventory** (format v1 or v2) and proposes which files belong together. It uses only the metadata already recorded: names, folders, extensions, sizes, timestamps and project markers.

It never opens your files, never rescans folders, never calls an AI model or any other service, and stores its results only in your data folder.

### What it proposes

- **A hierarchy:** course or term collections (for example `Fall 2021` → `CS 101 Intro`) containing assignments and projects.
  - Course and term folders are recognized from course codes, term names and words such as "course" or "class".
- **Assignments within one folder.** Loose files are split by:
  - assignment numbers: `hw1.py` and `hw1_writeup.pdf` together, `hw2.py` separately; different numbers are never joined
  - shared base names and shared name words
  - modification times that are close together
- **Project folders.** These are recognized by markers such as `.git`, a README, dependency files, IDE settings or a virtual environment.
  - Standard subfolders like `src` and `data` stay part of their project.
  - A clearly separate project nested inside another becomes a child group.
- **Suggested relationships across folders.** For example, a report in `Documents` and code in `PycharmProjects` that share uncommon name words and were active at the same time. These are **suggestions only**: groups are never merged unless you merge them.
- **Review flags, not decisions:**
  - possible duplicate copies (`- Copy`, `(2)`, matching paths and sizes); every copy is kept
  - possible third-party code: its own `.git` plus LICENSE/CONTRIBUTING files, timestamps that look bulk-copied, or a name on your own list. Ownership is never decided for you.
  - possible templates or starter code
  - generic names (`Untitled`, `main`)
  - large datasets
  - folders mixing several assignments
  - weak evidence

**Every file appears exactly once,** either in a group or in an "unassigned" list with the reason. Unassigned files come with the nearest likely groups.

**Every group, membership and relationship lists its evidence** with a confidence score (high, medium or low). Scores rank proposals; they are not probabilities.

**Generic names and copied timestamps carry little weight.** Files named `Untitled` or `main` don't contribute name evidence, and timestamps that look bulk-copied are ignored.

**Working titles come from folder and file names and are provisional.** Final repository names will come from the later, approval-gated naming stage.

### Reviewing and correcting

```powershell
python -m portfolioforge.grouping propose          # group the latest inventory
python -m portfolioforge.grouping list --flagged   # the hierarchy, with flags
python -m portfolioforge.grouping show <GROUP>     # evidence, files, flags, relationships
python -m portfolioforge.grouping merge <GROUP> <GROUP>
python -m portfolioforge.grouping split <GROUP> <FILE>... --title "Lab report"
python -m portfolioforge.grouping move <FILE> <GROUP>      # or: unassign <FILE>
python -m portfolioforge.grouping accept|reject|rename <GROUP> ...
python -m portfolioforge.grouping label <GROUP> mine|third_party|ignored
python -m portfolioforge.grouping keep-both|mark-duplicate <GROUP> <GROUP>
python -m portfolioforge.grouping confirm|dismiss <RELATIONSHIP>
python -m portfolioforge.grouping report           # writes a local Markdown report
```

- **Your decisions are kept** in a local ledger (`<data dir>/reviews/<inventory id>/decisions.jsonl`). They are re-applied every time proposals are regenerated, because group ids are stable.
- **Out-of-date decisions are reported,** never silently dropped: for example, one that refers to a file no longer in the inventory.
- **`--redact`** shows ids, kinds, counts and scores only.
- **`--inventory <ID>`** chooses an inventory other than the latest.

### Grouping settings

All rules use general defaults. To extend them for your own folders or language, add a `grouping.toml` in your settings folder:

```toml
[vocabulary]
assignment_words = ["aufgabe"]          # added to hw, lab, assignment, project, tarea, ...
collection_words = ["kurs"]
container_names = ["Semester Work"]     # folders that collect unrelated work
known_third_party_names = ["some-library"]

[patterns]
course_code = '(?i)\b([A-Z]{2,5})[ _-]?(\d{3,4}[A-Z]?)\b'

[weights]
same_assignment_number = 3.0

[thresholds]
relationship = 3.0
```

### Limitations

- **Metadata only.** Grouping can't see imports, notebook contents or report text, so files with generic names stay ambiguous until you review them.
- **Timestamps are imperfect.** Copying, unzipping and cloud sync change modification times, so time is supporting evidence only.
- **Course-code matches are hints and can be wrong,** such as product names with numbers. You can tune the pattern and excluded words.
- **Duplicate detection compares names, paths and sizes, not contents.** Copies with renamed files or different sizes can be missed. Content hashing is a deferred, opt-in feature.
- **Third-party detection relies on folder signals,** so it can miss vendored code and can flag your own published repositories. It only asks you to review.
- **The rules have been tested only on synthetic examples.** Expect to tune the settings, or correct groups, on real inventories.
- **`data purge` doesn't yet cover grouping results.** It manages saved inventories only; grouping results and decisions live in `<data dir>/groupings` and `<data dir>/reviews`.

## Privacy and your data

PortfolioForge works with the names and locations of your personal files, so it is built to keep that information on your computer.

### Where your data is stored

| | Windows | macOS | Linux |
|---|---|---|---|
| Inventories and other results | `%LOCALAPPDATA%\PortfolioForge` | `~/Library/Application Support/PortfolioForge` | `$XDG_DATA_HOME/portfolioforge` (default `~/.local/share/portfolioforge`) |
| Settings, including `privacy.toml` | `%APPDATA%\PortfolioForge` | same as above | `$XDG_CONFIG_HOME/portfolioforge` (default `~/.config/portfolioforge`) |

`python -m portfolioforge.data where` shows the exact folders on your computer. You can override them with the `PORTFOLIOFORGE_DATA_DIR` and `PORTFOLIOFORGE_CONFIG_DIR` environment variables.

### What PortfolioForge guarantees

These are enforced by the code and checked by automated tests:

- **It never opens your files.** No file contents are read, executed, modified, moved or deleted; only directory listings are used. Tests run a full scan with file opening, writing, running programs and network connections all made to fail.
- **Nothing leaves your computer.** There are no network calls, no telemetry, no cloud connections and no AI calls. Cloud-only placeholders are never downloaded.
- **Results stay out of Git.** Results are written only to your data folder; PortfolioForge refuses any output folder inside a Git repository, and `.gitignore` also blocks `*.jsonl` and the results folders.
- **Sensitive names are never recorded.** Credential-looking files are skipped, and their names are redacted from every result.
- **Private folders are skipped entirely** (see below).
- **Whole-computer scans need your approval.** You see the plan first and must confirm; deleting saved results also asks first.

### Private folders

Private folders are **skipped entirely**: they are never listed, nothing inside them is recorded, and their names appear as `[private]` in results. A scan also refuses to start inside one.

These common sensitive folder names are private by default (matched exactly, ignoring case): `Taxes`, `Tax Returns`, `Tax Documents`, `Medical`, `Medical Records`, `Health Records`, `Bank Statements`, `Banking`, `Insurance`, `Passwords`, `Legal Documents`, `Immigration`, `Pay Stubs`, `Payslips`, `Personal Documents` and `Private`.

Add your own in `privacy.toml` in your settings folder. This file is personal: keep it out of any repository.

```toml
[private_folders]
paths = ["D:/Scans/Personal"]          # absolute folders, with everything inside
names = ["Job Applications"]           # folder names, wherever they appear
allow_default_names = ["Insurance"]    # default names that aren't private for you
use_default_names = true               # set to false to drop the defaults
```

### Managing your data

```powershell
python -m portfolioforge.data list              # saved inventories: ids, dates, counts (no paths)
python -m portfolioforge.data stats latest      # counts-only statistics (no paths or file names)
python -m portfolioforge.data purge <ID>        # delete one inventory (asks first)
python -m portfolioforge.data purge --older-than 90
python -m portfolioforge.data where             # where data and settings are stored
```

`list` and `stats` never print file names or paths, so their output is safe to share.

`purge` deletes only PortfolioForge's own output files. It leaves alone any folder containing anything else, and never touches your original files.

### Limitations: what PortfolioForge cannot guarantee

- **Your results are personal data.** Inventories contain the names and paths of your files. They are stored unencrypted, and anyone with access to your account or administrator rights on the computer can read them.
- **Other software can read results files.** AI features in editors (for example GitHub Copilot in VS Code), clipboard tools and backup or sync tools can read or copy files you open. Avoid opening results files in an editor with AI extensions enabled, and exclude your PortfolioForge data folder from such tools.
- **Sharing is up to you.** If you paste terminal output, results files or reports into a chat, an issue or an AI assistant, that information leaves your computer. Use `--redact`, `data list` and `data stats` when you need to share something.
- **Name-based filters can miss private folders.** Private folders and credential files are recognized by name, so a private folder with an unusual name is only skipped if you add it to `privacy.toml`.
- **"Never opens files" is a code guarantee, not an operating-system sandbox.** It is enforced by the design, by code review and by tests; Python cannot sandbox itself.
- **Future AI analysis will be opt-in.** It isn't part of this release. When added, it will require separate approval of the specific files, and will show exactly what would be sent to which AI provider before anything is sent. Once data reaches a provider, its retention policy applies.

### For contributors

`python -m portfolioforge.devtools.privacy_check` scans everything Git would commit for:
- personal data files such as inventories, `privacy.toml` and datasets
- absolute paths into real users' home folders
- your own username and home path
- any private terms you list in `PORTFOLIOFORGE_PRIVATE_TERMS` or in `private-terms.txt` in your settings folder
- secrets

Findings show only the file, line and rule, never the matched text. CI runs the check with `--ci`, which skips the username and home-path rule because it only makes sense on your own computer.

To run it automatically before every commit, enable the hook once per clone:

```powershell
git config core.hooksPath .githooks
```

Tests and documentation must use synthetic, generic data only, such as `C:\Users\me` or a simulated `student` account.

## Roadmap

1. **Discover**: inventory local files *(implemented)*, then Google Drive and Google Colab.
2. **Group**: work out which files belong together, including files spread across folders.
3. **Analyze**: work out what each project does, what technologies it uses, and how complete it is.
4. **Document**: generate a README, summary, setup instructions and highlights for each project.
5. **Publish**: prepare each project as a clean repository and, after you approve it, publish it to GitHub.

## Guiding principles

- **Human in the loop**: Nothing is published or changed on an external service without explicit approval.
- **Privacy first**: Secrets, credentials, personal data and large datasets are detected and excluded before anything leaves the machine.
- **Non-destructive**: Original files are never modified, moved or deleted.

## Development

Requires Python 3.11+ (CI runs 3.13).

```powershell
pip install -e ".[dev]"
pytest -v
python -m portfolioforge.devtools.privacy_check
```

Tests run only against temporary folders and a simulated computer. They never scan your real files.

## Project layout

```
PortfolioForge/
├── README.md
├── pyproject.toml
├── .github/workflows/tests.yml     # CI: privacy check + pytest on Python 3.13
├── .githooks/pre-commit            # optional local privacy check before each commit
├── src/portfolioforge/
│   ├── paths.py                    # per-platform data/settings folders; refuses Git repos
│   ├── privacy.py                  # private-folder settings (privacy.toml)
│   ├── data.py                     # python -m portfolioforge.data (list/stats/purge/where)
│   ├── devtools/privacy_check.py   # repository privacy check
│   ├── grouping/                   # metadata-only candidate grouping (python -m portfolioforge.grouping)
│   │   ├── features.py, index.py   # name features; folder tree from the saved inventory
│   │   ├── segment.py, clustering.py   # course → assignment/project hierarchy; loose-file splitting
│   │   ├── linking.py, duplicates.py, flags.py   # suggested relationships, duplicates, review flags
│   │   ├── review.py, store.py     # persistent decisions; local storage outside Git
│   │   └── engine.py, report.py, cli.py, config.py, models.py
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
├── docs/design/                    # approved designs for future stages
└── tests/
```
