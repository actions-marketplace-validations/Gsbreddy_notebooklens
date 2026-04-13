# Quick Start: Hosted Review Workspace Beta

Use this path if your DS/ML team wants a richer notebook review surface than a PR comment can provide.

NotebookLens `v0.4.0-beta` adds a separate hosted review workspace that opens from a dedicated GitHub check run. This path is still **beta**.

The hosted workspace is a separate product surface from the OSS Action:

- the OSS Action owns the sticky PR comment
- the hosted workspace beta owns the `NotebookLens Review Workspace` check run
- both can exist on the same pull request

If you want the fastest first install inside GitHub, use [quickstart-action.md](quickstart-action.md) first.

## What you get

In the current `v0.4.0-beta` hosted workspace, reviewers can use:

- notebook-aware diffs
- snapshot history across pull request pushes
- inline thread create, reply, resolve, and reopen flows
- one-way GitHub PR sync for hosted thread activity
- installation-scoped LiteLLM settings for managed review
- a supported Docker Compose self-hosting path for internal pilots

Want to preview the hosted flow before setup? Use [examples.md](examples.md).

## Team quick start

This is the shortest evaluator flow for a DS/ML team:

1. Install the NotebookLens GitHub App on the repositories you want to review.
2. Sign in to NotebookLens with GitHub OAuth.
3. Open or update a pull request with `.ipynb` changes.
4. Open the `NotebookLens Review Workspace` check run.
5. Review the latest snapshot, then create or reply to inline threads in the hosted UI.

What reviewers should see:

- a dedicated `NotebookLens Review Workspace` check run on the pull request
- an `Open in NotebookLens` link
- latest snapshot status and thread counts
- hosted snapshot history and inline thread controls once the workspace opens

## What stays beta in `v0.4.0-beta`

This beta does **not** add:

- commit-only review
- standalone notebook conversations outside pull requests
- Helm or Kubernetes packaging
- bidirectional GitHub sync back into NotebookLens
- per-repo AI overrides

## Operator and admin paths

If your team wants to run the hosted workspace itself or configure managed review, use these docs:

- [Self-hosting runbook](self-hosting.md) to deploy the stack and finish GitHub App + OAuth wiring
- [LiteLLM admin settings](admin-ai-settings.md) to add installation-scoped managed AI after the stack is healthy
- [GitHub PR sync behavior](github-pr-sync.md) to understand how hosted thread activity appears back in GitHub

NotebookLens currently supports one managed deployment path for internal pilots:

- Docker Compose on a single host
- GitHub.com and GitHub Enterprise Server (`3.20.0+`)
- one shared public origin through `APP_BASE_URL`

Recommended operator order:

1. Deploy with [self-hosting.md](self-hosting.md)
2. Configure managed AI with [admin-ai-settings.md](admin-ai-settings.md) if needed
3. Review GitHub mirror behavior with [github-pr-sync.md](github-pr-sync.md)

## Privacy and storage

The hosted workspace beta keeps the UI responsive by storing versioned normalized review snapshots for 90 days by default.

Those snapshots include review-ready notebook diff material such as:

- changed-cell source text
- limited neighboring context
- output and metadata summaries
- deterministic findings
- reviewer guidance
- stable thread anchors

NotebookLens does not store untouched full notebook revisions wholesale for the hosted beta.

For broader privacy details across the Action and hosted workspace surfaces, see [privacy.md](privacy.md).

## Troubleshooting

Start with [troubleshooting.md](troubleshooting.md) if:

- the check run does not appear
- the hosted link fails to open
- reviewers cannot sign in with GitHub OAuth
- GitHub PR sync looks delayed or falls back to the workspace comment

For deployment-specific issues, use:

- [self-hosting.md](self-hosting.md)
- [admin-ai-settings.md](admin-ai-settings.md)
- [github-pr-sync.md](github-pr-sync.md)
