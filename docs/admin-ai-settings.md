# Admin Guide: LiteLLM Settings

NotebookLens `v0.4.0-beta` supports one managed AI gateway shape: an installation-scoped LiteLLM configuration.

## Use this guide when

Use this page after the managed workspace is already deployed and reviewers can open hosted reviews.

If you still need to bring up the stack or finish GitHub App + OAuth wiring, start with [self-hosting.md](self-hosting.md).

## Where this fits

The managed AI setup path is:

1. Deploy the hosted workspace with [self-hosting.md](self-hosting.md).
2. Configure LiteLLM from this page if your installation should use managed AI review.
3. Use [github-pr-sync.md](github-pr-sync.md) if you also need to understand how hosted review activity mirrors back into GitHub.
4. Use [troubleshooting.md](troubleshooting.md) if connection tests or managed review fallbacks do not behave as expected.

## Scope and permissions

- The setting is installation-scoped, not per repository.
- `provider_kind` is either `none` or `litellm`.
- An installation admin is the GitHub App installer for a user-owned installation or an organization owner for an org-owned installation.
- Testing a connection does not activate the gateway. Saving an active config does.

## Before you start

Confirm these runtime flags are enabled in the stack:

```dotenv
MANAGED_AI_UI_ENABLED=true
MANAGED_AI_GATEWAY_KIND=litellm
```

You also need:

- a reachable LiteLLM-compatible base URL
- a model name exposed by that gateway
- the API key and any optional static routing headers your gateway requires

For the higher-level evaluator flow, see [quickstart-workspace.md](quickstart-workspace.md). For privacy and storage behavior, see [privacy.md](privacy.md).

## What the admin UI stores

The settings page captures:

- display name
- model name
- base URL
- API key header name
- API key
- optional LiteLLM virtual key id
- GitHub host kind, API base URL, and web base URL for the installation
- optional static headers
- whether the gateway should use an OpenAI Responses-compatible endpoint
- whether the config is active

Secrets are stored encrypted. Read APIs redact secret values and only expose whether a secret is already present.

## Recommended setup flow

1. Open a managed review for the installation and navigate to the LiteLLM settings page.
2. Enter the gateway identity values first: display name, model name, base URL, API key header, and API key.
3. Match the GitHub host values to the installation owner:
   - `github_com` for GitHub.com
   - `ghes` plus the instance-specific API/web URLs for GitHub Enterprise Server
4. Add optional static headers only if your gateway requires tenant or routing headers.
5. Use `Test connection` before saving.
6. Save the config only after the connection test succeeds.
7. Enable `active` only when you want managed reviews to call LiteLLM for new or rebuilt snapshots.

## Runtime behavior

- Managed review remains deterministic by default when no active config exists.
- If an active LiteLLM gateway errors, NotebookLens records a visible notice and falls back to deterministic review.
- The gateway applies at the installation level, so all repositories under that installation share the same config.

## Current `v0.4.0-beta` limits

- No per-repo overrides
- No end-user prompt editing
- No managed prompt library
- No broader provider catalog beyond LiteLLM

## Troubleshooting

- Connection test fails: verify the base URL, API key header name, API key value, and any required static headers.
- GitHub host values do not match the installation: update the GitHub host kind plus API/web URLs before saving.
- Reviews keep falling back deterministically: check the stored gateway configuration and worker logs for LiteLLM request failures.

For broader deployment or sync troubleshooting, continue with:

- [self-hosting.md](self-hosting.md)
- [github-pr-sync.md](github-pr-sync.md)
- [troubleshooting.md](troubleshooting.md)
