# Design: Project Understanding and Naming (future stage)

Status: **approved design, partly implemented.**
- **Implemented** (`portfolioforge.understanding`):
  - the eligibility filter
  - Gate 1 file approval
  - local evidence extraction and redaction
  - the disclosure manifest with Gate 2 approval
  - the send gate and a dry-run provider
- **Not implemented:**
  - real AI providers
  - AI understanding and claim verification
  - naming, collisions and provenance
  - publishing

Nothing in PortfolioForge sends file contents to an AI provider.

## Goal

Published repositories get **descriptive, professional names** based on what each project verifiably does, never just its original file or folder names (`HW2.py`, `MLproject200`, `FinalAssignment.ipynb`, `pythonProject1`). Each project gets:
- a GitHub repository slug
- a readable title
- a short technical description

## Approved decisions

1. **No default AI provider.** Analysis is unavailable until the user configures one.
2. **No homework numbers or course codes in repository names by default.** This is configurable.
3. **Published files may be renamed only with user approval,** and every rename is recorded in a provenance record.
4. **Checking the user's existing GitHub repository names for collisions** is an opt-in step before publishing.

## Pipeline and approval gates

```
Grouping (metadata only; provisional working titles)
  → user accepts groups
  → GATE 1: user selects which files of an accepted group may be analyzed
  → local evidence extraction (no AI, nothing executed)
  → GATE 2: user approves a disclosure manifest (exact payload, provider, model)
  → AI understanding → local verification → name suggestions
  → user reviews and edits
  → GATE 3: user approves names, files and attribution
  → publishing preparation
  → GATE 4: user approves publishing
```

- **Working titles can never be published.** Grouping produces `WorkingTitle(text, provisional=True)`. The publisher accepts only an approved `ProjectIdentity`, a separate type that only the naming review step creates.

## Local evidence extraction (after Gate 1)

Only the user-selected files are read, locally, and nothing is executed. The extracted evidence:

| Source | What is extracted |
|---|---|
| Python | imports, functions and classes with docstrings and line numbers, using Python's built-in `ast` module |
| Notebooks | markdown and code cells; outputs only if the user allows them |
| Other languages | imports and `library()` calls, top-level definitions, comments (pattern-based) |
| Dependency files | declared packages |
| Data files | optional schema only (column names, row counts), never values |

- **Technologies are detected locally** by matching imports to a table. Every detected technology carries its file and line.
- **Redaction before anything can be sent:**
  - secrets
  - email addresses
  - absolute paths
  - name and author headers, which are flagged for the user to keep or remove
  - large literals
  - notebook outputs, unless the user allowed them

## Disclosure manifest (Gate 2)

The manifest lists:
- each excerpt, with its project-relative path, line ranges, size and the redactions applied
- everything excluded
- the provider and model
- a retention notice

The approved manifest is stored together with a hash of the exact payload. The provider adapter refuses to send anything whose hash differs. A local audit log records every send.

**Provider adapters:**
- **Anthropic** (Claude models), using the user's own API key from an environment variable or the system keychain, never from a settings file in a repository
- **local model**, for users who want nothing to leave the computer
- **dry-run**, which builds the payload and manifest but sends nothing

## AI understanding and local verification

- **The AI must answer in a fixed structured format:** `ProjectUnderstanding` with `purpose`, `functionality`, `algorithms`, `technologies`, `results`, `limitations` and `open_questions`.
- **Every claim cites evidence ids** from the payload: `Claim(text, evidence_ids, status)`.

**Local verification rejects:**
- claims without valid citations
- technologies that weren't detected locally and don't appear in the cited lines
- numbers (accuracy, scores, timings, dataset sizes) that don't appear word for word in the cited excerpt
- results claims without cited outputs or report text

**"Insufficient evidence" is a valid outcome.** Source content is treated as data, never as instructions: any prompt-injection text it contains has no effect and is flagged.

## Naming

Names are built only from verified claims. Several candidates are offered, each with its rationale and evidence.

| Field | Rules |
|---|---|
| Slug | `[a-z0-9._-]`, at most 100 characters, kebab-case by default. Leaves out homework numbers, course codes, IDE default names and personal names by default. |
| Title | Readable. |
| Description | At most 350 characters; every phrase traces to a verified claim. |
| Topics | Optional. GitHub-valid: lowercase, at most 50 characters each, at most 20. Verified technologies and domains only. |

- **A non-AI fallback** fills naming templates from locally verified technologies.
- **Naming conventions are configurable:** case, prefix, banned words, whether to include a language hint.

## Collisions

- **Checked against:**
  - the other projects in the same review
  - a local ledger of names already approved or published
  - optionally, the user's existing GitHub repository names: fetched with separate opt-in, names only, before publishing
- **Comparison follows GitHub's rules:** case-insensitive, with GitHub's normalization.
- **Resolution:** prefer a more specific descriptive alternative; a numeric suffix is the last resort.

## Provenance and attribution

Each project has a `ProvenanceRecord`:
- original folder names, file names and relative paths
- the mapping from original to published paths
- original timestamps
- authorship confirmed by the user: mine, team project, includes instructor starter code, or includes third-party code (with license)

Rules:
- Files keep their original names unless the user approves a rename. The mapping goes in `PROVENANCE.md` or a manifest.
- The generated README has an "Origin" section using wording the user approves.
- Third-party code is attributed with its license, or left out.
- An academic-integrity confirmation is required before publishing coursework.

## Review

- **Status flow:** `suggested → edited → approved`.
- **Every edit is checked again** for slug validity and collisions.
- **Text the user adds that isn't backed by evidence** is labeled "your wording".
- **Suggestions, AI responses and decisions stay in the user's local data folder.** Only approved text reaches a published repository, at Gate 4.

## Tests (planned)

All tests use synthetic data and no network:
- **A fake provider returns made-up claims;** verification must reject each one.
- **Tampering:** a payload that differs from the approved manifest isn't sent.
- **Redaction** removes emails, paths, secrets and notebook outputs.
- **Working titles** are rejected by the publisher.
- **Slug rules** are checked against many generated names.
- **Collisions** are detected despite case differences and normalization.
- **Provenance** mappings survive renames.
- **Prompt injection** in synthetic comments doesn't change the outcome.
- **Fallback naming** works without a provider.

## Limitations

- **Verification shows a claim is grounded in real code, not that it is correct.** The user's review is the final check.
- **Excluding notebook outputs and data hides results.** Results claims then say "not evidenced" instead of guessing.
- **Data sent to a provider is subject to that provider's retention policy.** The local-model option avoids this, usually at lower quality.
