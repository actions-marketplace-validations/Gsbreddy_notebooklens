# Quick Start: OSS Action

Use this path if your team wants the fastest way to add notebook-aware pull request review inside GitHub.

This is the recommended first install for most DS/ML teams because it:

- works on GitHub-hosted `ubuntu-latest`
- needs no extra service to deploy
- works in `ai-provider: none`
- keeps the review surface inside one sticky PR comment

If you want the separate hosted review workspace with snapshot history and inline threads, start with [quickstart-workspace.md](quickstart-workspace.md) instead.

## What you get

After install, NotebookLens adds one sticky PR comment on pull requests with changed `.ipynb` files.

That comment can include:

- notebook change summaries
- flagged findings
- notebook-local reviewer guidance
- optional Claude summary output when `ai-provider: claude` succeeds

Want to see a realistic static comment example before you install? Use [examples.md](examples.md).

## 1. Add the workflow

Create a workflow like this:

```yaml
name: NotebookLens

on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  contents: read
  pull-requests: write

jobs:
  notebooklens:
    runs-on: ubuntu-latest
    steps:
      - id: notebooklens
        name: Run NotebookLens
        uses: notebooklens/notebooklens@v0
        env:
          GITHUB_TOKEN: ${{ github.token }}
        with:
          ai-provider: none
          redact-secrets: true
          redact-emails: true
```

`GITHUB_TOKEN` is the built-in GitHub Actions token. NotebookLens uses it to read changed notebook metadata and create or update the sticky review comment.

If you prefer to copy from a checked-in example instead of the snippet above, use the workflow file in the repository root:
`/.github/notebooklens-pr.example.yml`.

## 2. Open or update a pull request

NotebookLens runs on supported `pull_request` events:

- `opened`
- `synchronize`
- `reopened`

The workflow only reviews pull requests that contain changed `.ipynb` files.

## 3. Confirm the comment appears

On a supported pull request, reviewers should see:

- one sticky NotebookLens comment
- notebook-local changed-cell summaries
- reviewer guidance, even in `none` mode
- notices when NotebookLens skips large or malformed notebooks

If notebook changes disappear from later pushes, NotebookLens deletes its own marker comment automatically.

## Optional next steps

After the baseline `none` mode flow is useful, you can add optional capabilities:

- Enable Claude by changing the workflow to:

```yaml
- name: Run NotebookLens (Claude mode)
  uses: notebooklens/notebooklens@v0
  env:
    GITHUB_TOKEN: ${{ github.token }}
  with:
    ai-provider: claude
    ai-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    redact-secrets: true
    redact-emails: true
```

- Add repo-specific reviewer prompts with `.github/notebooklens.yml`:

```yaml
version: 1
reviewer_guidance:
  playbooks:
    - name: Training notebooks
      paths:
        - "notebooks/training/**/*.ipynb"
      prompts:
        - "Verify the dataset split and random seed changes are intentional."
        - "Check whether metric changes are explained in markdown or the PR description."
```

- Inspect a realistic comment shape on [examples.md](examples.md).

## Privacy and limits

Key defaults for the Action path:

- `none` mode makes no external AI calls
- `claude` mode sends redacted review payloads to Anthropic
- NotebookLens never checks out the repository during the Action run
- Hard limits and deterministic behavior are documented in [privacy.md](privacy.md).

## Comment format at a glance

The sticky PR comment can include:

- notebook change summaries
- notebook-local reviewer guidance
- flagged findings
- an optional Claude summary block when Claude succeeds

For a full static example, use [examples.md](examples.md).

## Troubleshooting

For common setup issues, start with [troubleshooting.md](troubleshooting.md).

The most common first-run misses are:

- no `.ipynb` files changed in the pull request
- missing `contents: read` or `pull-requests: write`
- forgetting to pass `GITHUB_TOKEN` through `env:`
- expecting Claude output on fork PRs where secrets are unavailable
