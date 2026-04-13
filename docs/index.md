# NotebookLens Docs

NotebookLens helps DS/ML teams review Jupyter notebook changes on GitHub.

It has two product surfaces:

- the **OSS GitHub Action**, which adds one sticky pull request comment
- the **Hosted Review Workspace Beta**, which opens from a dedicated check run

## Start here

If you are new to NotebookLens, start with one of these two paths:

- **Fastest install:** [Quick Start: OSS Action](quickstart-action.md)
- **Deeper review workflow:** [Quick Start: Hosted Review Workspace Beta](quickstart-workspace.md)
- **Trust details first:** [Privacy and Data Flow](privacy.md) and [Troubleshooting](troubleshooting.md)

## Choose your path

| Path | Best for | What reviewers see in GitHub |
|---|---|---|
| OSS Action | Teams that want notebook-aware review in a few minutes with no extra service to run | One sticky NotebookLens PR comment |
| Hosted Review Workspace Beta | Teams that want snapshot history, inline threads, GitHub PR sync, and an optional self-hosted deployment | One `NotebookLens Review Workspace` check run that opens the hosted UI |

You can use both on the same pull request. They do not replace each other:

- the Action owns the sticky comment
- the hosted workspace owns the check run

## What most teams should do first

1. Start with the [OSS Action quick start](quickstart-action.md).
2. Use [Examples](examples.md) if you want to preview the output before installing anything.
3. Add the [Hosted Review Workspace Beta](quickstart-workspace.md) only when your review loop needs more than a PR comment.

## Common questions

- **Does NotebookLens need an external AI provider?** No. The OSS Action works in `ai-provider: none`, and the hosted workspace stays deterministic when no LiteLLM config is active. See [Privacy and Data Flow](privacy.md).
- **Will fork pull requests still work?** Yes, with different constraints for the Action and hosted workspace. See [Privacy and Data Flow](privacy.md#fork-pull-requests).
- **What if something does not show up in GitHub?** Start with [Troubleshooting](troubleshooting.md).
- **Can we self-host the hosted workspace beta?** Yes, for internal pilots with Docker Compose. Start with [Self-Hosting](self-hosting.md).

## Operator guides

If your team is running the hosted workspace itself, continue with:

- [Self-Hosting](self-hosting.md)
- [LiteLLM Admin Settings](admin-ai-settings.md)
- [GitHub PR Sync](github-pr-sync.md)

## Examples

Want to see the review surfaces before installing anything? Use [Examples](examples.md).
