# PortfolioForge

PortfolioForge is an agentic AI system that turns a scattered history of software engineering and machine learning work into a clean, published portfolio.

Past projects, especially academic ones, tend to be scattered: course folders on a laptop, `PycharmProjects`, `Downloads`, a personal Google Drive, and Google Colab notebooks. Often they have no README, no dependency file and no version control, and one course folder may hold several unrelated assignments. PortfolioForge aims to find these files, understand them, document them, and publish them to GitHub.

## Status

| Capability | Status |
|---|---|
| **Local file inventory** (targeted folders or the whole computer) | ✅ Implemented |
| **Privacy safeguards and local data management** | ✅ Implemented |
| **Candidate grouping** of files into courses, assignments and projects (metadata only, reviewed by you) | ✅ Implemented |
| **Analysis preparation**: eligibility filter, file approval, local evidence extraction, redaction, disclosure manifest, dry-run provider | ✅ Implemented |
| **AI project understanding and naming** (Anthropic, approval-gated, locally verified; [design](docs/design/project-understanding-and-naming.md)) | ✅ Implemented, opt-in; never runs without your approvals |
| **Local build-and-run inspection** (separately approved; Java is the only project type with an inspector) | ✅ Implemented |
| **Optional local validation** (compile or run on a temporary copy, each approved separately; not a sandbox) | ✅ Implemented, opt-in; runs nothing without your typed approval |
| AI analysis of code, notebooks and documents | 🗓️ Planned |
| **Google Drive and Google Colab discovery** (read-only; contents downloaded only after typed approval) | ✅ Implemented, opt-in |
| Documentation generation | 🗓️ Planned |
| **Publishing to GitHub** (one new repository per project, after checks and typed approval; private by default) | ✅ Implemented, opt-in |
| **Batch processing** (many projects, one consolidated review; private only) | ✅ Implemented, opt-in |

Nothing in PortfolioForge contacts an AI model unless you explicitly approve a disclosure manifest and then confirm sending it. Nothing is published unless you approve that exact publication, by typing it.

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

## Preparing projects for analysis

This stage prepares accepted groups for a future AI analysis, without connecting to any AI provider. It follows the [approved design](docs/design/project-understanding-and-naming.md) and has two approval gates.

```powershell
python -m portfolioforge.understanding eligibility             # which groups look like technical projects
python -m portfolioforge.understanding select <GROUP>          # files that could be read, and what is excluded
python -m portfolioforge.understanding approve-files <GROUP>   # GATE 1: allow reading them on this computer
python -m portfolioforge.understanding extract <GROUP>         # build a redacted, referenced evidence pack
python -m portfolioforge.understanding manifest <GROUP> --provider dry-run   # exactly what would be sent
python -m portfolioforge.understanding approve-manifest <GROUP> <MANIFEST>   # GATE 2: approve that exact payload
python -m portfolioforge.understanding dry-run <GROUP> <MANIFEST>            # test the send path; sends nothing
```

1. **Eligibility uses metadata only and reads no files.** Each group gets one of four statuses:
   - **`technical_candidate`**: code or notebooks, good grouping confidence, no review flags
   - **`needs_review`**: uncertain, for example a single script beside a report, documents only, loose files, low confidence, or flags for duplicates, third-party code, templates or generic names. Approving one requires `--acknowledge-review`. Little code alone never makes a group personal.
   - **`personal_or_admin`**: no code or notebooks, *and* names typical of personal paperwork (resume, transcript, tax, receipt, bank, …). These can't be approved.
   - **`excluded`**: groups you labelled ignored or third-party, and course collections

   Nothing is ever approved or selected automatically, and no group is marked as yours or publishable.
2. **Gate 1, file approval.**
   - Only groups you've **accepted** in the grouping stage can be approved.
   - Proposed files are Python, Java, R, R Markdown, SQL and notebooks, plus README and `requirements.txt`. Data files, documents, archives and images are excluded.
   - Credential-looking files, private folders and credential locations can never be approved.
   - The approval records each file's size and modification time. If a file changes afterwards, it can't be read until you approve it again.
3. **Local evidence extraction.**
   - Only approved files are opened, through a single guarded reader that refuses links and cloud-only placeholders. Nothing is executed: Python is parsed with the standard `ast` module, and other languages are scanned line by line.
   - Notebook outputs are excluded.
   - Each excerpt keeps its file, its line or cell, and a SHA-256 hash, so future AI claims can be checked against it.
   - Technologies are detected locally, only from imports and declared dependencies.
4. **Redaction.** Email addresses, absolute paths, secret-looking values, author and student-name lines, phone numbers and long literal strings are removed from excerpts before they are stored.
5. **Gate 2, the disclosure manifest.** It shows:
   - the provider and model
   - every excerpt, with its location, size and redactions
   - what is *not* sent: absolute paths, folder names above the project, the working title, timestamps, notebook outputs, data and documents
   - the payload size, an estimate of tokens, and the payload's SHA-256

   The exact payload is saved locally for you to inspect. Your approval covers that exact payload, provider and model; if anything changes, nothing is sent.
6. **The provider.**
   - Only the **dry-run** provider exists, and it sends nothing. There is no default provider and no network code.
   - Every attempt, including refusals, is recorded in a local audit log, without any content.

Approvals, evidence packs, manifests, payloads and the audit log are stored in `<data dir>/understanding/<inventory id>/<group id>/`, never in a Git repository. Evidence and payloads contain excerpts of your code, so keep them private. Use `--redact` for output that is safe to share.

**Limitations:**
- **The eligibility filter is a conservative heuristic.**
  - Coursework that is mostly documents lands in `needs_review`, which you can approve manually.
  - A project whose file names happen to look like personal paperwork, with no code, is treated as `personal_or_admin`.
- **Extraction for Java, R and SQL is pattern-based.** It can miss unusual syntax.
- **Python files that don't parse fall back to imports and definition lines only.**
- **Redaction is pattern-based** and can miss unusual personal details, so review the manifest and payload before approving.
- **Data schema extraction and document reading aren't implemented yet.**

## Local build-and-run inspection

When code excerpts aren't enough to say how a project builds and runs, this stage reads a few more approved files on your computer. It runs nothing and sends nothing.

```powershell
python -m portfolioforge.understanding select-build-files <GROUP>    # checks a fixed list of names; opens nothing
python -m portfolioforge.understanding approve-build-files <GROUP>   # separate approval to read those files
python -m portfolioforge.understanding inspect <GROUP>               # entry points, packages, versions, commands, sensitive data
python -m portfolioforge.understanding show-inspection <GROUP>
python -m portfolioforge.understanding apply-inspection <GROUP> <PROPOSAL>   # improve the draft README; nothing is sent
```

**Inspectors.** A small inspector for each project type supplies its build-file names, how to read them, its commands, allowed tools and any extra sensitive-data rules. The approvals, validation gates and README workflow are the same for every type. Java (NetBeans, Ant, Maven, Gradle and plain `javac`) is the only inspector so far.

**No inspector means "manual review needed".** No commands or validation plans are offered, and nothing about the project is assumed. Approved files still get the privacy scan, and a proposal gets a review flag.

**Supported or guess.** Every command is labelled:
- **Supported:** an inspected file backs it. For example, `ant run` needs `nbproject/build-impl.xml`, where NetBeans defines that target, as well as a main class in `nbproject/project.properties`.
- **Guess:** the inspected files don't confirm it, however plausible it looks.

Neither label means the command was run.

**Sensitive data.** The scan looks for likely credentials, identifiers and personal sample data, and reports the file, line and kind, never the value. It covers only the files that were read, and it lists what it didn't read, so a clean result says nothing about those. Findings become review flags, so accepting a proposal needs `--acknowledge-flags`. They never appear in the README. `nbproject/private` and files such as `config.properties` are never read.

**A new disclosure.** `manifest <GROUP> --provider ... --with-inspection` adds confirmed findings to a new payload. Its hash changes, so earlier approvals don't cover it. Guesses, scan results, risk signals and validation output are left out.

### Optional local validation

```powershell
python -m portfolioforge.understanding plan-validation <GROUP>   # compile, run and test status, and each plan; or --action compile
python -m portfolioforge.understanding approve-validation <GROUP> <PLAN>          # no --yes: you always type it
python -m portfolioforge.understanding validate <GROUP> <PLAN>
```

- **A project type offers only the checks its inspector supports.** Others are reported as unavailable, as is everything for a project with no inspector. Java offers compile and run, because tests would need libraries that aren't copied.
- **Results reach the published files.** `apply-inspection` records them, even on an accepted proposal, and `python -m portfolioforge.publishing refresh <GROUP> <STAGE>` regenerates `README.md` and `PROVENANCE.md` in a new stage. Acceptances carry over only where the file's bytes are unchanged.
- **Each plan shows its exact commands.** An approval covers that plan once. Approving `compile` doesn't authorize `run`, and `run` needs you to type `run my code`.
- **Nothing runs if anything changed.** The plan's hash, its commands and each source file's hash are checked again first.
- **It works on a temporary copy of the approved sources,** deleted afterwards, with a reduced environment, no shell, no input, a time limit and capped, redacted output.
- **For Java,** only `javac` and `java` run, never Ant, Maven or Gradle, whose build files are programs themselves. `javac` runs with annotation processing off, so compiling doesn't run project code.

> **This is not a sandbox.** A `run` uses your account's permissions, so the program could read or change your files or use the network if its code does so. Only approve code you're willing to run.

Exit code 0 is recorded as exactly that, not as proof the program works.

## AI project understanding and naming (optional)

The optional `ai` extra adds one AI provider, Anthropic. It sends nothing unless you approve a manifest *for Anthropic*, for the exact payload and model, and then type `send`.

```powershell
pip install -e ".[ai]"     # optional: the Anthropic SDK
# Credentials: ANTHROPIC_API_KEY, an `ant auth login` profile, or PORTFOLIOFORGE_ANTHROPIC_API_KEY.
# PortfolioForge never stores, prints or logs them.
python -m portfolioforge.understanding manifest <GROUP> --provider anthropic   # default: claude-sonnet-5-5, effort medium
python -m portfolioforge.understanding manifest <GROUP> --provider anthropic --model claude-opus-5-5   # optional: Opus
python -m portfolioforge.understanding approve-manifest <GROUP> <MANIFEST>   # review first; marked EXTERNAL
python -m portfolioforge.understanding submit <GROUP> <MANIFEST>             # asks you to type 'send'
python -m portfolioforge.understanding show-proposal <GROUP> <PROPOSAL> [--claims]
python -m portfolioforge.understanding readme <GROUP> <PROPOSAL>             # writes the draft README locally
python -m portfolioforge.understanding edit-proposal <GROUP> <PROPOSAL> --title "..." --name my-repo-name
python -m portfolioforge.understanding accept-proposal <GROUP> <PROPOSAL> --name <SLUG> [--acknowledge-flags]
```

**What is sent.**
- The approved payload *is* the complete request: the model, `max_tokens`, effort, a fixed system prompt, a fixed output schema, and the redacted evidence excerpts.
- The adapter sends exactly those fields; nothing else is added.
- Every approval is bound to one payload hash, one provider and one model. A dry-run approval can never authorize an Anthropic submission.
- The manifest shows every component of the request: the fields, model, `max_tokens`, effort, and the SHA-256 of the system prompt, the output schema and the evidence message. Changing any of them produces a different hash, so it needs a new manifest and a new approval. Editing a saved manifest or payload invalidates the approval too.
- No fallback model is used, so only the model you approved ever sees the content.
- **Cost:** the manifest and the final `send` prompt show a conservative *upper-bound* estimate. It's based on the model's published price, about 1 token per 3 bytes of payload plus structured-output overhead, and the full `max_tokens` of output (thinking counts toward it). It isn't an exact charge; check current pricing.

**What comes back.** A structured answer covering:
- purpose and key functionality
- languages and technologies
- engineering concepts
- uncertainties and possible third-party or template code
- 2–3 repository name suggestions, a title and a summary
- a draft README
- a setup, run and test guide

**Every claim is verified locally against the excerpts that were sent.** Rejected claims are shown for review but never used:
- claims citing no evidence, or evidence that wasn't sent
- numbers or outcome wording ("accuracy", "achieves", "outperforms") not found in the cited excerpts
- technologies not in imports or dependencies
- any authorship or first-person wording

**Repository names** must be valid GitHub names and descriptive. Homework numbers, course codes, IDE default names and words like "assignment" or "lab" are rejected.

**The README's setup, run and test sections** cover the *original* project:
- steps backed by the project's own files are marked **✓**, and steps those files don't confirm are marked **⚠ Unverified suggestion**, with a legend in the README
- invented dependencies are rejected
- commands mentioning files or tools that aren't in the evidence are marked **⚠ Unverified suggestion**
- for older projects, a compatibility section explains how to run the code as written (original versions in a separate environment), with modern alternatives listed only as suggestions
- claims that tests pass are rejected
- credentials are redacted
- compatibility notes separate the original requirement from a suggested modern alternative, which is never applied

When the excerpts aren't enough to tell how to run or test the project, the proposal is flagged for an additional approved local inspection. The README always states that its instructions haven't been run.

**Review and ownership.**
- Proposals, raw responses and README drafts stay in your data folder.
- Your edits are labelled as your wording.
- Accepting a proposal records the name you chose. It never creates a repository or publishes anything.
- Ownership is never inferred: proposals always say `ownership: unconfirmed`.

**Live tests are opt-in.** `PORTFOLIOFORGE_RUN_NETWORK_TESTS=1 pytest -m network` sends a tiny *synthetic* evidence pack to Anthropic. All other tests use mocked responses and block network access.

## Publishing to GitHub

This stage publishes one accepted project to its own new repository, using your existing GitHub CLI login. It works the same for any language.

```powershell
python -m portfolioforge.publishing candidates <GROUP>     # files you may select; nothing is selected by default
python -m portfolioforge.publishing stage <GROUP> <PROPOSAL> --file <ID> --file <ID> --ownership mine --license none
python -m portfolioforge.publishing preview <GROUP> <STAGE>   # the exact files, name, visibility, README and provenance
python -m portfolioforge.publishing review <GROUP> <STAGE>    # unresolved findings by file and line, with their IDs
python -m portfolioforge.publishing open-finding <GROUP> <STAGE> <FINDING>   # open that file at that line in VS Code
python -m portfolioforge.publishing resolve <GROUP> <STAGE> <FINDING> [<FINDING> ...] --reason "..."   # accept the ones you checked
python -m portfolioforge.publishing approve <GROUP> <STAGE>   # no --yes: you always type it
python -m portfolioforge.publishing publish <GROUP> <STAGE>
```

- **Only what you name is included.** Selected files are copied unchanged into a staging folder in your data folder, outside any repository, through the same guarded reader as every other stage. `README.md` and `PROVENANCE.md` are generated there. Credential-looking files and private folders can't be selected.
- **High-risk findings block publication.** The checks run on the staged bytes and cover privacy, sensitive data, ownership, third-party attribution and licensing. Anything that could identify a person (including author lines and identifiers in file paths) blocks, and so does anything that can't be scanned, such as a PDF or a notebook's saved outputs. Real secrets (private keys, access tokens) can only be fixed. Other findings, such as a false alarm, can be accepted one at a time with a recorded reason. Ownership and a license choice must be stated. `--coursework` records that history in `PROVENANCE.md`.
- **Private by default.** `--visibility public` is a separate choice, and approving it needs a different typed phrase. PortfolioForge can't stop a repository being made public later on GitHub itself.
- **Existing repositories are never touched.** The name is checked at preview and again just before publishing. Only a clear "not found" counts as free, and the workflow only ever creates a new repository. GitHub is told only the name until you publish.
- **Approval is single-use.** It covers that exact stage, name, visibility and GitHub account. Before uploading, every file's hash and all the checks run again, and the commit itself must hold exactly the approved bytes. Approval can't be given with `--redact`.

Your git name and email go into the commit. A public repository makes them public, so an email that isn't a GitHub noreply address blocks public publication until you change it or accept it.

**Limitations:**
- **The checks are pattern-based,** so they can miss things, such as a person's name in a file name. The preview is your final check.
- **Publishing can't be undone by PortfolioForge.** Treat anything published publicly as permanently exposed, even if you delete the repository later.
- **If the upload fails after the repository was created,** an empty repository may remain, and you'd need to delete it yourself before retrying that name.
- **Editing the README** means changing the proposal before accepting it; the staged README can't be edited.

## Google Drive and Google Colab discovery

Colab keeps notebooks in Google Drive as ordinary Drive files, and they're found by Drive's file API, so one read-only Drive integration covers both. Notebooks you only keep on GitHub, or never saved to Drive, aren't found. A Drive scan produces a normal inventory, so grouping, the batch workflow and every approval work as they do for local files.

**Two separate permissions, both read-only:**

| | Google permission | What it allows | Asked for by |
|---|---|---|---|
| Discovery | `drive.metadata.readonly` | Names, sizes, dates and checksums. It can't download file contents. | `connect` |
| Approved downloads | `drive.readonly` | Downloading file contents. PortfolioForge uses it only for files you approve. | `connect --content` |

Each has its own token, and a sign-in that grants anything other than the one permission asked for is discarded. You can do all your discovery and duplicate checks without ever granting the second one. Neither permission can change your Drive, and PortfolioForge's Drive client can only issue read requests. `drive.file` isn't used because it only sees files you pick one by one, so it can't discover anything.

**Setting up (once).** PortfolioForge ships no shared Google credentials, so you use your own free Google Cloud project: create a project, enable the Drive API, configure the OAuth consent screen with yourself as a test user, add the two scopes above, and create an OAuth client of type *Desktop app*. Download its JSON file, then:

```powershell
python -m portfolioforge.drive connect --client-file <downloaded file>   # discovery permission only
```

While a Google Cloud project is in "Testing" status, Google expires its sign-ins after 7 days; run `connect` again when asked.

**Finding projects:**

```powershell
python -m portfolioforge.drive scan --colab-only                # notebooks, and the folders that lead to them
python -m portfolioforge.drive scan --folder "Coursework"       # one top-level folder (or a folder's ID); repeatable
python -m portfolioforge.drive scan                             # all of My Drive; asks first
python -m portfolioforge.drive scan --plan-only                 # show the plan, and stop
python -m portfolioforge.drive duplicates                       # compare with a local inventory again
python -m portfolioforge.drive status                           # which permissions are held
python -m portfolioforge.drive disconnect --revoke              # delete the sign-ins, and cancel them at Google
```

**Then the normal workflow:**

```powershell
python -m portfolioforge.batch --inventory latest-drive run --provider anthropic
python -m portfolioforge.batch --inventory latest-drive review
python -m portfolioforge.drive connect --content                # before your first approved download
python -m portfolioforge.batch --inventory latest-drive approve-read <GROUP> ...
```

`latest` always means your latest *local* inventory, so a Drive scan never takes over local work in progress. Use `latest-drive` or the inventory's ID.

- **The same rules as a local scan.** Credential and private folders (including your `privacy.toml` names), sensitive file names, shortcuts, environments, caches and build output are skipped, and sensitive and private names are redacted. Trashed items, shared drives and items you don't own are left out; `--include-shared` adds what's been shared with you.
- **Nothing is downloaded by a scan.** A file's contents are fetched only after you type the `download N` confirmation at `approve-read`, and only the files listed there. They go to a private cache in your data folder, and a file that changed after discovery isn't kept. `python -m portfolioforge.data purge` removes an inventory's downloads with it.
- **Sending stays a separate decision.** A download goes no further than your computer. Anything sent to Anthropic still needs you to approve that exact disclosure and type the `send` confirmation. Code is only run through approved validation, and nothing is published without its own typed approval.
- **Duplicates are suggestions.** A scan compares against your latest local inventory (`--compare-with` to choose another, `none` to skip) using names, paths and sizes, without opening any local file. Colab rewrites notebooks when saving, so the same distinctive notebook name at a different size counts as a possible variant. Matches appear in both batches' reviews and at `approve-read`; nothing is hidden or removed, and `exclude` sets a project aside. Text files copied between Windows and other systems can differ in size from line endings, so those show up as variants rather than duplicates.
- **Each notebook in a `Colab Notebooks` folder is its own candidate,** unless its name links it to another. The folder names are configurable as `standalone_notebook_folders` in `grouping.toml`.

**Limitations:**
- **Google Docs, Sheets and Slides aren't read.** They have no file to download, so they're counted in the scan summary but not inventoried.
- **Scanning all of My Drive, or using `--colab-only`, fetches all your Drive's metadata,** because Drive can't reliably filter by these rules itself. Only what passes them is saved. `--folder` lists only that folder's tree, and never lists inside a private folder.
- **`private_paths` in `privacy.toml` are local paths,** so they don't apply to Drive. Use folder names, or `--exclude NAME`.
- **A private folder you don't own can't be recognized.** Your own file inside someone else's folder appears under an `Other location` folder, because the parent's name isn't visible.
- **Publishing from Drive covers the files you approved for reading.** To publish other files of a Drive project, approve them first.
- **Checksums aren't compared against local files,** since that would mean opening them; duplicates rely on names, paths and sizes.

## Batch processing

This runs the stages above across all your candidate projects, so decisions are made together instead of project by project. It adds no new way to read, send, run or publish anything: each step goes through the same gates.

```powershell
python -m portfolioforge.batch run --provider anthropic   # prepare all that can be prepared; never asks, sends or runs
python -m portfolioforge.batch review                     # projects, names, readiness, validation, privacy, duplicates, decisions
python -m portfolioforge.batch approve-read <GROUP> <GROUP> ...      # allow reading these files locally
python -m portfolioforge.batch send <GROUP> <GROUP> ...              # send these exact disclosures to the AI provider
python -m portfolioforge.batch accept-names <GROUP> <GROUP>=<SLUG> ...
python -m portfolioforge.batch validate <GROUP> ... --action compile # or run
python -m portfolioforge.batch stage <GROUP> ... --ownership mine --license none
python -m portfolioforge.batch publish <GROUP> ...                   # previews each one, and asks for each one
python -m portfolioforge.batch status
python -m portfolioforge.batch resume                                # after an interruption
```

- **`run` and `resume` are safe to repeat.** A project's state is worked out from the existing records each time, so finished work, including earlier AI answers and published repositories, is recognized and never redone.
- **Automatic:** ordering candidates (likely engineering projects first), flagging possible duplicates, and, once you've allowed reading, local extraction, inspection, preparing disclosure manifests and validation plans, and regenerating staged documents when new results arrive.
- **Each decision lists what it covers and needs a typed confirmation** that includes the count. Every disclosure keeps its own approval, payload hash, provider and model; a changed payload isn't sent, and the others still go.
- **A send that ends unclearly is never repeated by itself.** After an interruption, timeout, connection failure or server error, the request may already have been processed or charged, so the project waits for your `send --resend`. PortfolioForge also turns off the Anthropic SDK's own automatic retries. An answer saved before an interruption is used without a new request.
- **You can set projects aside at any point** with `exclude` (for unrelated work or a duplicate) or `defer`, and bring them back with `restore`. Nothing is read or sent for them.
- **Every valid AI name suggestion is shown in the review.** The first is the visible default, and you can choose another.
- **Publishing is private only, and each repository is previewed and approved individually.** A taken name is never overwritten. One project's problem doesn't stop the others.
- **Program output stays out of the batch output,** and stays in a redacted local record.
- **Sensitive findings use the existing commands:** `python -m portfolioforge.publishing review`, `open-finding` and `resolve`.

**Limitations:**
- **Scanning isn't included.** Run the inventory scan or [Drive scan](#google-drive-and-google-colab-discovery) first; `run` works from the latest local inventory and its grouping, or the one you name with `--inventory`.
- **Only one batch command should run at a time.** There is no lock between terminals.
- **Only a provider refusal that shows the request was never accepted (such as HTTP 400 or 429) lets a project be sent normally again.** The provider's own usage page is the final word on charges.
- **Staging uses the files you approved for reading.** To publish fewer, or data files, use `python -m portfolioforge.publishing stage` for that project.
- **A statement given to `stage` covers every project named in that command,** so run it separately for projects whose ownership or license differs.

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
- **Nothing leaves your computer without your approval.** There's no telemetry, and cloud-only placeholders are never downloaded. The Anthropic request needs file approval, approval of the exact payload for that provider and model, and a typed `send` confirmation. Publishing needs its own approval. The optional Google Drive connection is read-only, runs only when you ask for it, and sends no project content to Google; its results stay on your computer.
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
- **Once data reaches an AI provider, that provider's retention policy applies.** AI analysis is opt-in and approval-gated, but anything you send can't be recalled. Review the manifest and the saved payload before approving.
- **Redaction and verification are pattern-based.** They reduce, but can't eliminate, the chance that a personal detail in a code comment is sent, or that a subtly wrong description slips through. Your review is the final check.

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

1. **Discover**: inventory local files and Google Drive, including Colab notebooks *(implemented)*.
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
│   ├── understanding/              # analysis preparation (python -m portfolioforge.understanding)
│   │   ├── eligibility.py, selection.py   # metadata-only eligibility; Gate 1 file approval
│   │   ├── reader.py, extract.py, redact.py   # guarded reader; evidence extraction; redaction
│   │   ├── disclosure.py           # manifest, Gate 2 approval, send gate, dry-run provider
│   │   ├── anthropic_provider.py   # Anthropic adapter: sends exactly the approved request body
│   │   ├── analysis.py             # output schema, local claim/instruction verification, proposals
│   │   ├── build_files.py, inspection.py, sensitive.py   # build-file approval; local inspection; privacy scan
│   │   ├── inspectors/             # inspector interface, registry, and the Java inspector
│   │   ├── validation.py           # the only code that runs a project; approval-gated
│   │   ├── findings.py             # local findings into proposals and new manifests
│   │   └── store.py, models.py, cli.py
│   ├── publishing/                 # staging, blocking checks, approval and GitHub CLI upload
│   ├── batch/                      # job queue and consolidated review over the stages above
│   ├── drive/                      # read-only Google Drive and Colab discovery (python -m portfolioforge.drive)
│   │   ├── auth.py, client.py      # two read-only permissions; a client that can only issue GET requests
│   │   ├── discovery.py            # Drive metadata into a normal inventory, under the local scanner's rules
│   │   ├── hydrate.py              # downloads approved files to a private cache, after typed approval
│   │   ├── duplicates.py           # possible duplicates between Drive and local inventories
│   │   └── cli.py
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
