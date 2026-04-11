# NotebookLens

GitHub-native notebook PR triage for Jupyter. `none` mode works with zero external AI calls; Claude is optional.

NotebookLens runs as a Docker GitHub Action on `ubuntu-latest`, detects `.ipynb` changes, and maintains one idempotent PR comment (`<!-- notebooklens-comment -->`) with notebook/cell summaries, output-change summaries, optional flagged findings, and optional Claude summary details.

## v0.1.0 Public Surface

- `ai-provider`: `none | claude` (default: `none`)
- `ai-api-key`: used only when `ai-provider=claude`
- `redact-secrets`: default `true`
- `redact-emails`: default `true`
- Packaging/runtime: Docker action, GitHub-hosted `ubuntu-latest` only

Out of scope for `v0.1.0`: OpenAI/Ollama providers, GitLab/Bitbucket, hosted review UI, inline notebook threads, or extra public Action inputs.

## Quick Start (`none` first)

Use this first. It needs no AI key and sends nothing to external model providers.

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
      - name: Run NotebookLens (none mode)
        uses: your-org/notebooklens@v0
        env:
          GITHUB_TOKEN: ${{ github.token }}
        with:
          ai-provider: none
          redact-secrets: true
          redact-emails: true
```

## Enable Claude (second step)

After `none` mode is useful for your PR triage flow, enable Claude for richer summary/findings.

```yaml
- name: Run NotebookLens (Claude mode)
  uses: your-org/notebooklens@v0
  env:
    GITHUB_TOKEN: ${{ github.token }}
  with:
    ai-provider: claude
    ai-api-key: ${{ secrets.AI_API_KEY }}
    redact-secrets: true
    redact-emails: true
```

If `ai-provider=claude` is requested without a key, or Claude fails, the run degrades safely to `none` mode and adds a visible notice in the PR comment.

## Privacy Note

- In `none` mode, NotebookLens performs local diff/review logic only and does not call external AI APIs.
- In `claude` mode, NotebookLens sends redaction-processed review payload data to Anthropic.
- Redaction is best effort. It targets secret-like values, sensitive assignments, connection strings, long base64 blobs, and optional email addresses.
- Notebook binary outputs are never forwarded as raw payloads; output handling is summarized/truncated for review.

If your policy disallows third-party model calls, keep `ai-provider: none`.

## Troubleshooting

`No comment appears on a PR`
- Confirm event is `pull_request` with one of: `opened`, `synchronize`, `reopened`.
- Confirm at least one changed file ends in `.ipynb`.
- Confirm workflow/job permissions include `contents: read` and `pull-requests: write`.
- Confirm `GITHUB_TOKEN` is passed to the action environment.

`Expected Claude output, but got none-mode behavior`
- Check `ai-provider` is exactly `claude`.
- Check `ai-api-key` is present and valid.
- For fork PRs, secret availability may be restricted; fallback to `none` is expected and is surfaced in notices.

`Existing NotebookLens comment did not update as expected`
- NotebookLens only updates/deletes marker comments it owns (bot-authored comment containing `<!-- notebooklens-comment -->`).
- If notebook changes are removed from later commits in the PR, NotebookLens deletes its marker comment by design.

`Large/malformed notebook behavior`
- Notebooks over size/cell limits are skipped or truncated deterministically with explicit notices.
- Malformed or unreadable notebook JSON is surfaced through notices while processing continues for other notebooks.

## Example Workflow File

See [.github/notebooklens-pr.example.yml](.github/notebooklens-pr.example.yml) for a runnable baseline.

## License

MIT. See [LICENSE](LICENSE).
