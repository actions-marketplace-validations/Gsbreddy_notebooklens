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
        uses: Gsbreddy/notebooklens@v0
        env:
          GITHUB_TOKEN: ${{ github.token }}
        with:
          ai-provider: none
          redact-secrets: true
          redact-emails: true
```

`GITHUB_TOKEN` is the built-in GitHub Actions token. NotebookLens uses it to read changed notebook metadata and create or update the sticky review comment.

You can also start from the checked-in workflow example in the repo:
[`/.github/notebooklens-pr.example.yml`](https://github.com/Gsbreddy/notebooklens/blob/main/.github/notebooklens-pr.example.yml).

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

- Enable Claude with the repo README section:
  [Enable Claude (optional)](https://github.com/Gsbreddy/notebooklens/blob/main/README.md#enable-claude-optional)
- Add repo-specific reviewer prompts with the README playbooks section:
  [Reviewer Guidance Playbooks](https://github.com/Gsbreddy/notebooklens/blob/main/README.md#reviewer-guidance-playbooks)
- Inspect the exact comment format in the README:
  [PR Comment Format](https://github.com/Gsbreddy/notebooklens/blob/main/README.md#pr-comment-format)

## Privacy and limits

Key defaults for the Action path:

- `none` mode makes no external AI calls
- `claude` mode sends redacted review payloads to Anthropic
- NotebookLens never checks out the repository during the Action run
- Hard limits and deterministic behavior are documented in [privacy.md](privacy.md).

## Troubleshooting

For common setup issues, start with [troubleshooting.md](troubleshooting.md).

The most common first-run misses are:

- no `.ipynb` files changed in the pull request
- missing `contents: read` or `pull-requests: write`
- forgetting to pass `GITHUB_TOKEN` through `env:`
- expecting Claude output on fork PRs where secrets are unavailable
