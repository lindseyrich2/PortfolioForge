# PortfolioForge

**Finds your old software and data projects, works out what they do, documents them, and publishes them as private GitHub repositories for you to review.**

Old projects end up scattered across course folders, `Downloads`, Google Drive, Colab and GitHub, usually with no README and names like `hw3_final2`. PortfolioForge turns them into documented repositories without inventing claims or exposing private data, and it asks for your approval before anything leaves your computer.

## How it works

```text
Discover -> Group -> Read approved files -> Approve disclosure -> AI analysis -> Verify claims
   -> Name, README, provenance -> Privacy and attribution checks -> Approve scope -> Publish PRIVATE
```

| Stage | What it does |
|---|---|
| **Discover** | Lists files from a local scan, Google Drive and Colab, or your GitHub repositories, from metadata alone |
| **Group** | Sorts files into candidate projects and flags possible duplicates across sources |
| **Understand** | Extracts redacted evidence locally, then sends only that exact approved payload to Anthropic |
| **Verify** | Checks each AI claim against the evidence it cites, and rejects what it can't support |
| **Document** | Writes a project-specific README and a `PROVENANCE.md` recording origin, attribution and what was actually run |
| **Publish** | Blocks on privacy findings, never overwrites a repository, and only creates private repositories in a batch |

## Safety

Enforced in code and covered by tests:

- **Nothing leaves without approval.** Each AI request is bound to the hash of the exact payload, provider and model. A send that ends unclearly is never retried automatically.
- **Original files are never changed.** Publishing works from a staged copy, and the commit is checked against the approved bytes.
- **Code doesn't run by itself.** Compiling or running a project needs a separate typed approval, and it isn't a sandbox.
- **Publishing is private.** A batch can't create a public repository; making one public is your own separate decision.
- **Secrets stay out.** Sensitive file names and private folders are skipped, values are redacted before disclosure, and likely secrets block publication until you review them.
- **Google and GitHub access is read-only for discovery.** Drive uses a metadata-only permission, with a second permission needed to download approved files. GitHub discovery never fetches file contents.
- **Your data stays local.** Inventories and results live in your per-user data folder, and PortfolioForge refuses to write them inside a Git repository.

Ownership is your statement, and attribution labels are signals: commit history doesn't prove who wrote the code.

## Setup

Requires Python 3.11+. The [GitHub CLI](https://cli.github.com/) is needed for GitHub discovery and publishing.

```powershell
pip install -e ".[ai,dev]"
$env:ANTHROPIC_API_KEY = "<your key>"     # only needed to send approved disclosures
gh auth login                             # only needed for GitHub
```

## Usage

**1. Discover from any of these sources.** Each one writes its own inventory.

```powershell
python -m portfolioforge.inventory "C:\path\to\projects"     # local folders, or --full-computer
python -m portfolioforge.drive connect --client-file <file>  # once; see Google setup below
python -m portfolioforge.drive scan --colab-only             # or --folder NAME, or all of My Drive
python -m portfolioforge.github scan                         # repositories you can reach
```

**2. Work through one consolidated review.** Add `--inventory latest-drive`, `latest-github` or an inventory ID; the default is your latest local scan.

```powershell
python -m portfolioforge.batch run --provider anthropic      # prepares what it can; never sends or publishes
python -m portfolioforge.batch review                        # everything that needs you
python -m portfolioforge.batch approve-read <GROUP> ...      # read these files locally
python -m portfolioforge.batch send <GROUP> ...              # send these exact disclosures
python -m portfolioforge.batch accept-names <GROUP> ...      # choose repository names
python -m portfolioforge.batch stage <GROUP> ... --ownership mine --license none
python -m portfolioforge.batch publish <GROUP> ...           # one approval of an exact private list
```

`exclude`, `defer` and `restore` set projects aside and bring them back. `run` and `resume` are safe to repeat, and finished work is never redone.

**Manage your data:** `python -m portfolioforge.data where`, `list`, `stats` and `purge`.

### Google setup (once)

In a free Google Cloud project: enable the Drive API, configure the OAuth consent screen with yourself as a test user, add the `drive.metadata.readonly` scope, and create a *Desktop app* client. Download its JSON and pass it to `drive connect --client-file`. To analyze Drive files, also add `drive.readonly` and run `drive connect --content`. While the project is in Testing status, Google expires sign-ins after 7 days.

## Limitations

- **GitHub projects are discovery only.** To analyze one, clone it and run the local scan. The clone's attribution label doesn't carry over automatically, so check it when you state ownership.
- **Colab Recents has no public API.** A notebook opened from a repository your login isn't affiliated with can't be found without its URL.
- **Google Docs, Sheets and Slides aren't read,** and evidence extraction covers Python, notebooks, Java, R, R Markdown and SQL.
- **Duplicates compare names, paths and sizes,** not contents, so similar scripts with different names aren't caught.
- **Extensionless `LICENSE` files aren't inventoried.** A project that must keep one needs that file added to its approval record before staging.
- **GitHub limits repository creation.** A large publication can be rate limited; repeating `publish` later continues safely.
- **Only one batch command should run at a time.** There's no lock between terminals.

## Results

The first full run, on the maintainer's own projects:

| | |
|---|---|
| **Sources scanned** | A local computer (599 files), Google Drive and Colab (277 items, 3 notebooks), GitHub (16 repositories, 250 files) |
| **Candidate projects** | 126 from the local scan, plus 3 collaborative GitHub projects prepared from clones |
| **AI analyses** | 40 in the final batch, for at most $6.93, with every payload matching its approved hash and none retried |
| **Published** | 25 private repositories, each verified on GitHub as private; none made public |
| **Deferred, not guessed at** | 75, including 35 with no code, 13 with no verifiable name, and 12 with possible third-party code or an unconfirmed source |
| **Held for human review** | 3 collaborative projects, where the checks found possible private data |

Most of the projects are small scripts, so many of their READMEs are short and say what couldn't be verified. That's the intended result: no invented claims.

## Backlog

v1.0 is complete, and none of this is planned work:

- Carry a GitHub attribution label over to its local clone automatically.
- Inventory extensionless `LICENSE` files.
- Compare file contents to catch similar scripts with different names.
- Pause and resume publishing around GitHub's rate limit automatically.
- Read Google Docs, Sheets and Slides.
- Add language inspectors beyond Java.

## Development

```powershell
pytest
python -m portfolioforge.devtools.privacy_check
```

Tests use temporary folders and invented data. They never touch your real files, Drive or repositories. To run the privacy check before each commit: `git config core.hooksPath .githooks`.

```
src/portfolioforge/
├── inventory/      local metadata scan
├── drive/          read-only Google Drive and Colab discovery
├── github/         read-only GitHub discovery and attribution signals
├── grouping/       candidate projects and duplicates
├── understanding/  approvals, evidence, disclosure, AI analysis, verification, README
├── publishing/     staging, blocking checks and private upload
├── batch/          one queue and review across all of the above
└── data.py, paths.py, privacy.py, devtools/
```

MIT licensed.
