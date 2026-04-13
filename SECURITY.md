# Security Policy

## Reporting a Vulnerability

NotebookLens uses GitHub-native private vulnerability reporting.

- Open the repository's [Security Advisories](https://github.com/notebooklens/notebooklens/security/advisories) page.
- Use the **Report a vulnerability** flow to send a private report.
- Do **not** open a public issue for security-sensitive findings.

## What NotebookLens Processes

- Changed `.ipynb` files from GitHub pull requests
- Notebook cell source, outputs, and selected metadata
- Pull request file metadata needed to render the review comment

## Privacy and Redaction

Before any Claude request, NotebookLens redacts:

- URI credentials such as `scheme://user:pass@host`
- Connection strings for common databases and brokers
- Sensitive assignments such as `TOKEN=...`, `PASSWORD=...`, and `API_KEY=...`
- Long base64-like blobs
- Email addresses when `redact-emails: true`

Binary-style outputs are summarized by type and size rather than forwarded verbatim.

In `none` mode, no external AI request is made.

## GitHub Token Safety

- `GITHUB_TOKEN` is used only for GitHub API access required by the action.
- It is not forwarded to Anthropic or included in rendered PR comments.
- NotebookLens requires only `contents: read` and `pull-requests: write`.

## Supply Chain Notes

- NotebookLens runs as a Docker action on GitHub-hosted runners.
- The container image installs the action's declared Python dependencies at build time.
- The action performs static notebook inspection only; it does not execute notebook code.

## Supported Disclosure Path

Security fixes are shipped through normal GitHub releases. Watch the repository's releases page if you want notification of future patches.
