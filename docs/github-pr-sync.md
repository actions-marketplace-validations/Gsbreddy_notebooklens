# Admin Guide: GitHub PR Sync

NotebookLens `v0.4.0-beta` mirrors managed workspace thread activity into GitHub pull requests, while keeping the hosted workspace as the source of truth.

## Use this guide when

Use this page if your team wants to understand what the hosted workspace writes back to GitHub and why some thread activity becomes inline PR review comments while other activity stays in the workspace comment fallback.

If you are still setting up the deployment itself, start with [self-hosting.md](self-hosting.md). If you are only evaluating the managed beta flow, start with [quickstart-workspace.md](quickstart-workspace.md).

## Where this fits

The operator path around GitHub sync is:

1. Deploy and verify the workspace with [self-hosting.md](self-hosting.md).
2. Optional: configure managed LiteLLM review with [admin-ai-settings.md](admin-ai-settings.md).
3. Use this page to understand the GitHub mirror contract, authorship rules, fallback behavior, and UI status states.
4. Use [troubleshooting.md](troubleshooting.md) when sync status stays pending, fallback comments appear unexpectedly, or mirrored authorship does not match the acting reviewer.

## What sync does

- Maintains one app-authored workspace comment on the PR discussion tab
- Adds an `Open in NotebookLens` link plus latest snapshot and sync status
- Mirrors hosted thread creation into native GitHub PR review comments when NotebookLens can map the anchor to a stable `.ipynb` diff position
- Mirrors hosted replies onto the same GitHub PR thread
- Mirrors resolve and reopen events as NotebookLens bot replies

## What sync does not do

- It does not make GitHub the canonical discussion surface.
- It does not sync GitHub-native edits or replies back into NotebookLens.
- It does not create a fresh GitHub root comment for every carried-forward snapshot.

## Source-of-truth rule

NotebookLens records the hosted thread first. GitHub mirroring runs after that via background jobs.

Operationally, that means:

- reviewers should keep editing the discussion in NotebookLens
- GitHub is the visibility surface for PR-native collaborators
- mirror delays or failures do not delete the hosted thread state

## Mirroring modes

### Native PR review thread

NotebookLens uses a native GitHub PR review comment when the hosted anchor maps cleanly to the changed `.ipynb` diff.

This is the preferred path for:

- source blocks
- markdown blocks
- output/image blocks whose changed output still maps to concrete changed JSON lines

### Workspace-comment fallback

If NotebookLens cannot find a stable GitHub diff position, it updates the aggregated fallback section inside the app-owned workspace comment instead of forcing a broken inline anchor.

Fallback entries include:

- notebook path
- block kind and hosted status
- anchor context
- a direct link back to the hosted thread

This fallback is part of the normal `v0.4.0-beta` contract, not a data-loss path.

## Authorship behavior

- NotebookLens uses the acting reviewer's stored GitHub token when it is available and valid.
- If that token is missing, expired, or revoked, NotebookLens falls back to app-authored mirroring for that write.
- The app-owned workspace comment always stays bot-authored.

## Edit and delete behavior

- When a mirrored NotebookLens message is edited, NotebookLens updates the GitHub mirror in place.
- When a mirrored NotebookLens message is deleted, NotebookLens replaces the mirrored GitHub text with a short tombstone that points readers back to NotebookLens.

## Mirror status in the UI

Thread rows can show four high-level states:

- `Mirrored to GitHub`
- `GitHub sync pending`
- `GitHub sync skipped`
- `GitHub sync failed`

Those states are informational only. NotebookLens remains the canonical editable surface in every case.

## Operator checklist

1. Keep `GITHUB_PR_SYNC_ENABLED=true` in the managed stack.
2. Verify the GitHub App installation still has pull-request write access on the target repositories.
3. Encourage reviewers to keep their GitHub OAuth tokens fresh if you want mirrored authorship to match the acting reviewer.
4. Treat the workspace comment as the fallback surface for unmappable notebook anchors, not as an error by itself.

## Related docs

- [quickstart-workspace.md](quickstart-workspace.md)
- [self-hosting.md](self-hosting.md)
- [admin-ai-settings.md](admin-ai-settings.md)
- [privacy.md](privacy.md)
- [troubleshooting.md](troubleshooting.md)
