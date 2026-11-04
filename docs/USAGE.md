# PortfolioForge usage guide

## Setup

```powershell
git clone https://github.com/lindseyrich2/PortfolioForge.git
cd PortfolioForge
pip install -e ".[ai,dev]"
```

Configure only what you'll use:

| To use | You need |
|---|---|
| AI analysis | `ANTHROPIC_API_KEY` set in your environment |
| GitHub discovery or publishing | The [GitHub CLI](https://cli.github.com/) (`gh auth login`), and your Git name and email configured |
| Google Drive and Colab | Your own OAuth client, set up below |

Optional settings go in your settings folder, which `python -m portfolioforge.data where` shows. `privacy.toml` adds private folders, and `grouping.toml` adjusts grouping.

### Google Drive and Colab

Colab keeps notebooks in Drive, so one integration covers both. In a Google Cloud project:

1. Enable the Drive API.
2. Configure the OAuth consent screen, with yourself as a test user.
3. Add the `drive.metadata.readonly` scope.
4. Create a *Desktop app* client, and download its JSON file.

To analyze Drive files, also add the `drive.readonly` scope and run `drive connect --content`. Google expires sign-ins after 7 days while the project is in Testing status.

## Discovery

Each source writes its own inventory. `latest` always means your latest local scan.

| Source | Access | What's read |
|---|---|---|
| Local folders, or the whole computer | Directory listings | Metadata until you approve reading specific files |
| Google Drive and Colab | OAuth, `drive.metadata.readonly` | Metadata; a separate `drive.readonly` permission downloads approved files |
| GitHub repositories you can reach | Your `gh` sign-in, GET requests only | Metadata, ownership, fork status and commit counts; never file contents |

```powershell
python -m portfolioforge.inventory "C:\path\to\projects"     # or --full-computer, which shows a plan and asks first
python -m portfolioforge.drive connect --client-file <file>  # once
python -m portfolioforge.drive scan --colab-only             # or --folder NAME, or no option for all of My Drive
python -m portfolioforge.drive status                        # which permissions are held
python -m portfolioforge.drive disconnect --revoke           # delete the sign-ins, and cancel them at Google
python -m portfolioforge.github scan                         # asks first; or --repo OWNER/NAME
python -m portfolioforge.github scan --read-local-remotes    # also match local clones by their Git remote
```

## Batch workflow

The default is your latest local scan. For the others, add `--inventory latest-drive` or `--inventory latest-github` straight after `batch`.

```powershell
python -m portfolioforge.batch run --provider anthropic      # prepares what it can; never sends or publishes
python -m portfolioforge.batch review                        # everything that needs you, in one place
```

Each decision lists exactly what it covers, then asks for the confirmation shown:

| Command | Confirmation |
|---|---|
| `batch approve-read GROUP ... --acknowledge-review` | `read N` (`download N` for Drive) |
| `batch send GROUP ...` | `send N to anthropic` |
| `batch accept-names GROUP[=SLUG] ...` | `accept N` |
| `batch stage GROUP ... --ownership mine --license none` | `stage N` |
| `batch publish GROUP ...` | `publish N private` |

`--acknowledge-review` is only needed for projects marked `needs_review`. `--ownership` also takes `team`, `includes-starter-code` or `includes-third-party`, and `--license` takes an SPDX identifier.

Other commands:

```powershell
python -m portfolioforge.batch status                        # one line per project
python -m portfolioforge.batch resume                        # carry on after an interruption
python -m portfolioforge.batch defer GROUP --reason "..."    # also exclude and restore; reads nothing
python -m portfolioforge.batch send GROUP --resend           # only after a send that ended unclearly; may be charged again
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

## Testing

```powershell
pytest
python -m portfolioforge.devtools.privacy_check
git config core.hooksPath .githooks                          # optional: run the privacy check before each commit
```

Tests use temporary folders and invented data, and never touch your real files, Drive or repositories.

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
