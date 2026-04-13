# Troubleshooting

Use this page when NotebookLens is installed but the expected GitHub surface or hosted flow does not appear.

NotebookLens has two separate paths:

- the OSS Action, which owns one sticky PR comment
- the hosted review workspace `v0.4.0-beta`, which owns the `NotebookLens Review Workspace` check run

Start by confirming which path you are trying to use.

## OSS Action

### No comment appears on a pull request

Check these first:

- the workflow is triggered by `pull_request`
- the event action is one of `opened`, `synchronize`, or `reopened`
- at least one changed file ends in `.ipynb`
- the job permissions include `contents: read` and `pull-requests: write`
- `GITHUB_TOKEN` is passed through `env:`

If notebook changes disappear from a later push, NotebookLens deletes its marker comment by design.

### Expected Claude output, but got `none` mode behavior

Check:

- `ai-provider` is exactly `claude`
- `ai-api-key` is present and valid
- the run is not coming from a fork PR without access to repository secrets

When Claude is unavailable, NotebookLens falls back to `none` mode and records the reason in:

- a visible PR comment notice
- the `notebooklens.runtime` structured log line

### Reviewer playbooks did not apply

Confirm:

- the file path is exactly `.github/notebooklens.yml`
- the file uses `version: 1`
- each playbook has non-empty `name`, `paths`, and `prompts`

Also remember:

- NotebookLens reads config from the PR head revision
- renamed notebooks match against the current head path
- fork PRs still use the fork-side config when present

If the config is malformed, NotebookLens ignores playbooks, keeps built-in guidance enabled, and adds one visible notice.

### Large or malformed notebooks

Current shipped limits:

- first 20 notebooks per PR
- first 500 alignment rows per notebook
- 50 MB notebook size limit
- 16,000-token Claude payload budget
- 2,000 characters of output text inspected per output block

Expected behavior:

- oversize notebooks are skipped with notices
- malformed notebooks are surfaced through notices
- Claude payload shaping may compact or truncate AI input without a separate PR comment notice

### Import error or `ModuleNotFoundError`

If you are using the published action, this usually indicates a custom local image or build path rather than the published release itself.

Check that your local image build copies both:

- `pyproject.toml`
- `src/`

and installs dependencies before the runtime entrypoint.

## Hosted Review Workspace Beta

The hosted workspace is still **beta** in `v0.4.0-beta`.

### The check run does not appear

Check:

- the repository is installed in the NotebookLens GitHub App
- the pull request contains changed `.ipynb` files
- the managed deployment is healthy
- `MANAGED_REVIEW_BETA_ENABLED=true`
- the GitHub App webhook points to `$APP_BASE_URL/api/github/webhooks`

If the repository only uses the OSS Action, you will see the sticky PR comment but not the hosted workspace check run.

### The hosted link opens, but reviewers cannot access the workspace

Check:

- the reviewer has signed in with GitHub OAuth
- the reviewer still has access to the repository
- `APP_BASE_URL` is the same public origin used by the web and API routes
- the deployment is not serving stale OAuth callback or session settings

### Snapshot build or thread sync looks delayed

The hosted workspace uses background jobs for snapshot creation and GitHub PR sync.

Check:

- the `worker` service is running
- the database is healthy
- webhook delivery is succeeding
- GitHub PR sync is enabled when you expect mirrored activity

Temporary sync delay does not delete hosted thread state. NotebookLens remains the source of truth for hosted discussions.

### GitHub PR sync falls back to the workspace comment

That is expected when NotebookLens cannot map the hosted anchor to a stable native `.ipynb` diff position.

The current `v0.4.0-beta` behavior is:

- source and markdown blocks can use native PR review comments when line mapping is stable
- image and plot blocks can use native PR review comments only when changed output maps to concrete changed notebook JSON lines
- unmappable anchors are summarized in the aggregated fallback section of the app-owned workspace comment

### Mirrored authorship is bot-authored instead of reviewer-authored

NotebookLens prefers the acting reviewer’s stored GitHub token for mirrored writes.

If that token is missing, expired, or revoked, mirroring falls back to app-authored writes. The workspace comment always remains bot-authored.

## Self-hosted deployment

For operator setup/debug issues, continue with:

- [self-hosting.md](self-hosting.md)
- [admin-ai-settings.md](admin-ai-settings.md)
- [github-pr-sync.md](github-pr-sync.md)

Common checks:

- `docker compose ... ps`
- `curl -f \"$APP_BASE_URL/api/healthz\"`
- GitHub App webhook delivery status
- GitHub OAuth callback configuration
- LiteLLM connectivity and gateway credentials

## Structured logs

The OSS Action emits:

- `notebooklens.runtime`
- `notebooklens.comment_sync` when a run reaches comment sync

Use `notebooklens.runtime` to confirm:

- requested provider
- effective provider
- whether fallback happened
- fallback reason
- changed notebook count
- total changed cells

The hosted workspace path relies on deployment logs plus worker/job behavior rather than those Action-only stdout events.

## Where to go next

- Privacy, permissions, fork behavior, and limits: [privacy.md](privacy.md)
- OSS Action first-run path: [quickstart-action.md](quickstart-action.md)
- Hosted workspace beta evaluator path: [quickstart-workspace.md](quickstart-workspace.md)
