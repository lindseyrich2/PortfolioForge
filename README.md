# PortfolioForge

**Finds past software and data projects across a computer, Google Drive, Colab and GitHub, works out what each one does using an LLM with verified evidence, documents it, and publishes it to a private GitHub repository under explicit approvals.**

Years of projects tend to end up in course folders, `Downloads`, Drive and Colab, with no README and names like `hw3_final2`. Turning them into documented repositories by hand is slow, and handing them to an AI tool raises real risks: leaked credentials, invented claims, and other people's code presented as your own. PortfolioForge does the routine work and treats each of those risks as an engineering problem, with approval gates, local verification and blocking checks.

## Pipeline

```text
Discover -> Group -> Read approved files -> Extract and redact evidence -> Approve exact disclosure
   -> AI analysis -> Verify claims -> Name, README, provenance -> Privacy and attribution checks
   -> Stage -> Approve exact scope -> Publish PRIVATE -> Verify
```

| Source | Access | What's read |
|---|---|---|
| Local folders, or the whole computer | Directory listings | Metadata until you approve reading specific files |
| Google Drive and Colab notebooks | OAuth, `drive.metadata.readonly` | Metadata; a separate `drive.readonly` permission downloads approved files |
| GitHub repositories you can reach | Your `gh` sign-in, GET requests only | Metadata, ownership, fork status and commit counts; never file contents |

Colab keeps notebooks in Drive, so one integration covers both.

## Architecture

```
src/portfolioforge/
├── inventory/      metadata-only local scanner, with exclusion rules and platform handling
├── drive/          Drive client, OAuth, discovery, approved downloads, cross-source duplicates
├── github/         `gh` metadata client, repository discovery, attribution signals
├── grouping/       segments files into projects and assignments; duplicates, flags, your corrections
├── understanding/  eligibility, approvals, evidence extraction, redaction, disclosure, AI adapter,
│                   claim verification, README generation, build inspection, optional validation
├── publishing/     staging, blocking checks, private upload
├── batch/          one queue and one review across all stages
└── data.py, paths.py, privacy.py, devtools/
```

**Key design decisions:**

- **Every source becomes the same inventory.** Drive and GitHub listings go through one builder, under the local scanner's rules, so later stages don't care where a project came from.
- **Each disclosure is bound to a hash.** The approved payload is the complete Messages API request body. A send is refused if its hash, provider or model no longer match the approval.
- **AI output is treated as untrusted.** Output must match a schema, each claim must cite evidence IDs, and local verification rejects what it can't support. Setup steps are checked against the project's files.
- **Provenance is explicit.** Each published repository gets a `PROVENANCE.md` recording its origin, ownership statement, license and which commands were actually run.
- **State is derived, not stored.** A project's position comes from its saved approvals, proposals and stages, so interrupted runs resume without repeating work.

## Technology

| | |
|---|---|
| **Language** | Python 3.11+, using only the standard library in the core |
| **AI** | Anthropic Messages API through the `anthropic` SDK, optional; default model `claude-sonnet-5-5`; automatic retries turned off |
| **Google** | Drive API v3 over `urllib`, with OAuth loopback and PKCE, using your own Google Cloud client |
| **GitHub** | The GitHub CLI (`gh`), and Git for commits |
| **Storage** | JSON and JSON Lines in a per-user data folder, outside any Git repository |
| **Testing** | `pytest`, with GitHub Actions on Python 3.13 |

## Installation

```powershell
git clone https://github.com/lindseyrich2/PortfolioForge.git
cd PortfolioForge
pip install -e ".[ai,dev]"
```

Then configure only what you'll use:

| To use | You need |
|---|---|
| AI analysis | `ANTHROPIC_API_KEY` set in your environment |
| GitHub discovery or publishing | The [GitHub CLI](https://cli.github.com/) (`gh auth login`), and your Git name and email configured |
| Google Drive and Colab | Your own OAuth client, set up below |

**Google setup.** In a Google Cloud project: enable the Drive API, configure the OAuth consent screen with yourself as a test user, add the `drive.metadata.readonly` scope, and create a *Desktop app* client. Download its JSON file for `drive connect`. To analyze Drive files, also add `drive.readonly` and run `drive connect --content`. Google expires sign-ins after 7 days while the project is in Testing status.

Optional settings go in your settings folder, shown by `python -m portfolioforge.data where`. `privacy.toml` adds private folders, and `grouping.toml` adjusts grouping.

## Quick start

**1. Discover.** Each source writes its own inventory.

```powershell
python -m portfolioforge.inventory "C:\path\to\projects"     # or --full-computer, which shows a plan and asks first
python -m portfolioforge.drive connect --client-file <file>  # once
python -m portfolioforge.drive scan --colab-only             # or --folder NAME, or no option for all of My Drive
python -m portfolioforge.github scan                         # asks first; or --repo OWNER/NAME
```

**2. Prepare and review.** The default is your latest local scan. For the others, add `--inventory latest-drive` or `--inventory latest-github` straight after `batch`.

```powershell
python -m portfolioforge.batch run --provider anthropic      # prepares what it can; never sends or publishes
python -m portfolioforge.batch review                        # everything that needs you, in one place
```

**3. Approve each stage.** Each command lists exactly what it covers, then asks for the confirmation shown.

| Command | Confirmation |
|---|---|
| `batch approve-read GROUP ... --acknowledge-review` | `read N` (`download N` for Drive) |
| `batch send GROUP ...` | `send N to anthropic` |
| `batch accept-names GROUP[=SLUG] ...` | `accept N` |
| `batch stage GROUP ... --ownership mine --license none` | `stage N` |
| `batch publish GROUP ...` | `publish N private` |

`--acknowledge-review` is only needed for projects marked `needs_review`. `--ownership` also takes `team`, `includes-starter-code` or `includes-third-party`, and `--license` takes an SPDX identifier.

**Useful commands:**

```powershell
python -m portfolioforge.batch status                        # one line per project
python -m portfolioforge.batch resume                        # carry on after an interruption
python -m portfolioforge.batch defer GROUP --reason "..."    # also exclude and restore; reads nothing
python -m portfolioforge.batch validate GROUP --action compile   # optional, after typed approval; not a sandbox
python -m portfolioforge.data list                           # also stats, purge and where
```

## Automated versus authorized

| PortfolioForge does this automatically | You authorize this |
|---|---|
| Scanning metadata and applying exclusion rules | Google and GitHub sign-ins, and scanning all of My Drive, all repositories or a whole computer |
| Grouping, duplicate flags and attribution signals | Reading a project's files |
| Evidence extraction, redaction and disclosure preparation | Sending each exact disclosure to Anthropic |
| Verifying claims, and generating READMEs and provenance | Repository names |
| Privacy, attribution and licensing checks | Ownership and license statements |
| Staging, and refreshing documents when results change | Publishing an exact list of private repositories |
| Resuming and deferring unsafe items | Running any project code, and making anything public |

No command makes a repository public in a batch.

## Privacy and safety

Enforced in code and covered by tests:

- **Nothing leaves without approval.** Excerpts are redacted first; notebook outputs, data files and absolute paths aren't sent.
- **A send that ends unclearly is never retried automatically,** because it may already have been processed or charged.
- **Original files aren't changed.** Publishing works from a staged copy, and the commit is checked against the approved bytes.
- **Project code doesn't run by itself,** and validation runs on a temporary copy after separate approval.
- **Likely secrets block publication.** So do personal identifiers, unattributed third-party notices and a missing license file, until they're reviewed.
- **Existing repositories are never overwritten.** A name clash defers that project.
- **Personal data stays out of Git.** Sensitive file names and private folders are skipped, results stay in your data folder, and a privacy check runs in CI and as an optional pre-commit hook.

## Testing

```powershell
pytest
python -m portfolioforge.devtools.privacy_check
git config core.hooksPath .githooks                          # optional: run the privacy check before each commit
```

The suite has about 870 tests, using temporary folders and invented data; they never touch your real files, Drive or repositories. CI runs the privacy check and the suite on each push and pull request to `main`.

**Standalone acceptance test.** v1.0.0 was also tested as a user would run it: a fresh clone, a new virtual environment, isolated data folders, and real CLI processes with typed answers, against a fake GitHub CLI and a fake Anthropic server. Drive ran through its CLI with a simulated Google. The whole workflow passed, from discovery through private publishing, including declined approvals, a blocked fake credential, a name clash and a safe rerun. The one failure was a poor error message, listed under limitations.

## Results

The first full run, on the author's own projects:

| | |
|---|---|
| **Sources scanned** | A local computer (599 files), Google Drive and Colab (277 items, 3 notebooks), GitHub (16 repositories, 250 files) |
| **Candidate projects** | 126 from the local scan, plus 3 collaborative GitHub projects prepared from clones |
| **AI analyses** | 40 in the final batch, for at most $6.93, with every payload matching its approved hash and none retried |
| **Published** | 25 private repositories, each verified on GitHub as private; none made public |
| **Deferred, not guessed at** | 75, including 35 with no code, 13 with no verifiable name, and 12 with possible third-party code or an unconfirmed source |

**The main lesson was about quality, not safety.** Every published repository was accurate and passed its checks, but many were small scripts or near-duplicates that weren't worth showing anyone. A human audit of the source reduced the 25 to a handful of curated repositories. PortfolioForge has no measure of how substantial a project is, so that judgment stays with you.

## Limitations

- **No quality gate.** A tiny script can be published if it's safe, and a project with no extractable code can still be sent for analysis.
- **Duplicates compare names, paths and sizes,** not contents, so near-identical scripts with different names aren't caught.
- **GitHub projects are discovery only.** To analyze one, clone it and run the local scan; its attribution label doesn't carry over to the clone automatically.
- **Colab Recents has no public API,** so a notebook opened from a repository your login isn't affiliated with can't be found.
- **Google Docs, Sheets and Slides aren't read.** Evidence extraction covers Python, notebooks, Java, R, R Markdown and SQL, and only Java has a build inspector.
- **Extensionless `LICENSE` files aren't inventoried,** so a project that must keep one needs manual handling.
- **Large publications can hit GitHub's repository-creation limit.** Repeating `publish` later continues safely, but it doesn't wait by itself.
- **`accept-names` stops if any listed project has no valid name.** Name that project yourself or set it aside.
- **A missing API key gives a confusing error.** Nothing is sent, but the message doesn't mention `ANTHROPIC_API_KEY`.
- **Downloading Drive file contents hasn't been run against real Google,** only against a simulated one. Real Drive metadata, GitHub metadata, Anthropic and GitHub publishing have all been used.
- **Only one batch command should run at a time.** There's no lock between terminals.
- **Validation isn't a sandbox,** so review its plan before approving it.

## How it was built

Lindsey Rich set the goals, safety requirements and approval rules, reviewed the results and made the decisions about scope and publication. Most of the code, tests and documentation were written with [Claude Code](https://claude.com/claude-code), Anthropic's AI coding tool, working under that direction. Claude Code also operated the application during the first real run, including some curation done by hand outside the application.

## License

MIT. See [`LICENSE`](LICENSE).
