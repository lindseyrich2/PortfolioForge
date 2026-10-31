# PortfolioForge

PortfolioForge is an agentic AI system that turns a scattered history of software engineering and machine learning work into a clean, published portfolio.

Past projects tend to live in many places: local folders, Google Drive, and Google Colab notebooks. Often they have no documentation or version control and are hard to show to anyone. PortfolioForge aims to find them, understand them, document them, and publish them to GitHub.

## Goals

PortfolioForge is planned as a pipeline of cooperating agents:

1. **Discover**: Scan configured sources (local directories, Google Drive, Google Colab) and list candidate projects.
2. **Analyze**: Inspect each project's code, notebooks, data, and dependencies to work out what it does, what technologies it uses, and how complete it is.
3. **Document**: Generate useful documentation for each project, such as a README, a summary, setup instructions, and highlights.
4. **Publish**: Prepare each project as a clean repository and, after the user approves it, publish it to GitHub.

## Guiding principles

- **Human in the loop**: Nothing is published or changed on an external service without explicit approval.
- **Privacy first**: Secrets, credentials, personal data, and large datasets are detected and excluded before anything leaves the machine.
- **Non-destructive**: Source projects are read, never modified in place.

## Status

🚧 **Early setup.** Only the project scaffolding exists so far. No agents, integrations, or dependencies have been implemented.

## Requirements

- Python 3.11+

## Project layout

```
PortfolioForge/
├── README.md         # Project overview (this file)
├── pyproject.toml    # Project metadata and build configuration
└── .gitignore        # Files and folders excluded from version control
```
