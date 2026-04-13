# Privacy and Data Flow

NotebookLens has two separate product surfaces:

- the OSS GitHub Action
- the hosted review workspace `v0.4.0-beta`

They use different GitHub integrations and different storage paths, so teams should evaluate privacy and permissions separately for each path.

## OSS Action

### What it reads

The Action reads pull request file metadata plus base and head notebook content for changed `.ipynb` files.

It uses:

- the GitHub Files API to discover changed notebooks
- the GitHub Contents API to fetch notebook content

The Action does **not** check out the repository in the runner workspace.

### What it writes

The Action writes exactly one sticky PR comment identified by `<!-- notebooklens-comment -->`.

It updates that comment in place on later pushes and deletes it if notebook changes disappear from the pull request.

### External model calls

- In `ai-provider: none`, NotebookLens makes no external AI calls.
- In `ai-provider: claude`, NotebookLens sends a redaction-processed review payload to Anthropic.

If your policy disallows third-party model calls, keep `ai-provider: none`.

### Redaction behavior

Before any external AI call, NotebookLens applies best-effort redaction for:

- URI credentials such as `scheme://user:pass@host`
- connection strings for PostgreSQL, MySQL, MongoDB, Redis, AMQP, Snowflake, and JDBC-style DSNs
- sensitive assignments such as `TOKEN=`, `SECRET=`, `API_KEY=`, `PASSWORD=`, `PRIVATE_KEY=`, and `DSN=`
- long base64 blobs
- email addresses when `redact-emails: true`

Binary outputs such as images, HTML display payloads, and JSON display data are not forwarded verbatim. NotebookLens summarizes output type, size, and truncation state instead.

## Hosted Review Workspace Beta

The hosted review workspace is still **beta** in `v0.4.0-beta`.

### What it reads

The managed backend reads pull request metadata and notebook content through the GitHub App installation token for installed repositories.

Signed-in reviewers access the hosted workspace through GitHub OAuth plus repo-access checks.

### What it writes

The hosted workspace writes:

- the `NotebookLens Review Workspace` check run
- hosted thread state and snapshot metadata in NotebookLens-managed storage
- one-way GitHub PR sync for hosted thread activity when enabled

### What it stores

To keep the hosted UI responsive, NotebookLens stores versioned normalized review snapshots for 90 days by default.

Those snapshots can include:

- changed-cell source text
- limited neighboring context
- output summaries
- metadata summaries
- deterministic findings
- reviewer guidance
- stable thread anchors

NotebookLens does **not** store untouched full notebook revisions wholesale for the hosted beta.

Managed review in `v0.4.0-beta` can use installation-scoped LiteLLM settings when an active configuration exists. If no active LiteLLM configuration exists, or the configured gateway errors, NotebookLens continues with deterministic local review and records a visible notice. The hosted beta does not add managed Claude/OpenAI provider settings in this release.

### Email and session handling

- Hosted access is gated by encrypted GitHub OAuth sessions plus repository-access checks.
- Thread email notifications are limited to signed-in participants plus the PR author when a usable email is available.
- GitHub mirroring prefers user-scoped GitHub tokens and falls back to app-authored writes when the acting reviewer token is unavailable.

## Permissions

### OSS Action permissions

The Action path needs this workflow permission block:

```yaml
permissions:
  contents: read
  pull-requests: write
```

It also expects `GITHUB_TOKEN` to be passed through `env:`.

`contents: read` is used to read changed notebook metadata and notebook content. `pull-requests: write` is used to create, update, or delete the sticky NotebookLens PR comment.

### Hosted workspace permissions

The hosted workspace path uses:

- a GitHub App with repository installation access
- pull request write access for check runs and PR sync
- GitHub OAuth for reviewer identity and repo-access checks

Self-hosted deployments also need the operator-managed credentials documented in:

- [quickstart-workspace.md](quickstart-workspace.md)
- [self-hosting.md](self-hosting.md)
- [admin-ai-settings.md](admin-ai-settings.md)
- [github-pr-sync.md](github-pr-sync.md)

## Fork Pull Requests

Fork behavior is different between the two surfaces.

### OSS Action on fork PRs

GitHub Actions does not expose repository secrets to workflows triggered from fork pull requests in the normal `pull_request` flow.

That means:

- the Action still runs in `none` mode when the workflow permissions are correct
- `ai-provider: claude` falls back to `none` when the forked run has no `ai-api-key`
- the PR comment includes a visible notice when Claude falls back

Fork pull requests still use the PR head revision for `.github/notebooklens.yml`, so reviewer playbooks from the fork branch can still participate in guidance generation.

### Hosted workspace on fork PRs

The hosted workspace fetches notebook data through the GitHub App installation path rather than GitHub Actions secrets. As long as the repository is installed and the pull request is visible to the App, the hosted beta can build snapshots for fork-origin PRs.

## Hard Limits

These limits are part of the current shipped behavior:

| Limit | Value |
|---|---|
| Notebooks processed per PR | 20 (first 20 in GitHub file order; remainder skipped with notice) |
| Cells aligned per notebook | 500 (first 500 alignment rows; remainder skipped with notice) |
| Notebook size | 50 MB (notebooks over this size are skipped with notice) |
| AI input token budget | 16,000 tokens (payload compacted or truncated before Claude call) |
| Output text inspected for summaries | 2,000 characters per output block |

Behavior notes:

- Notebook-size and aligned-cell limits surface as notices while processing continues for remaining notebooks.
- The 16,000-token AI budget is enforced during Claude payload shaping and may compact or truncate the AI payload without a separate PR comment notice.
- Malformed notebook JSON is surfaced through notices while processing continues for other notebooks.

## Related Docs

- [quickstart-action.md](quickstart-action.md)
- [quickstart-workspace.md](quickstart-workspace.md)
- [troubleshooting.md](troubleshooting.md)
- [self-hosting.md](self-hosting.md)
- [admin-ai-settings.md](admin-ai-settings.md)
- [github-pr-sync.md](github-pr-sync.md)
