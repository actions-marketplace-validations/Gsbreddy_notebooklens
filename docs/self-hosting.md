# Self-Hosting the Managed Workspace

NotebookLens `v0.4.0-beta` supports one operator path for internal pilots: Docker Compose on a single host.

This runbook covers the managed PR review workspace only. The OSS GitHub Action remains unchanged and can run with or without the managed stack.

## Use this guide when

Use this page if your team wants to deploy the hosted review workspace itself.

If you are only evaluating the beta as a reviewer, start with [quickstart-workspace.md](quickstart-workspace.md) first.

## Operator path at a glance

The shortest operator path through the managed docs is:

1. Use this page to bring up the stack and finish GitHub App + OAuth wiring.
2. Use [admin-ai-settings.md](admin-ai-settings.md) if you want installation-scoped LiteLLM review.
3. Use [github-pr-sync.md](github-pr-sync.md) if you need to understand how hosted thread activity appears back in GitHub.
4. Use [troubleshooting.md](troubleshooting.md) if deployment health, sign-in, or sync behavior does not match expectations.

## What `v0.4.0-beta` supports

- Docker Compose deployment on a single host
- GitHub.com and GitHub Enterprise Server (`3.20.0+`)
- One public origin via `APP_BASE_URL`
- Separate `gateway`, `web`, `api`, `worker`, and `postgres` services
- Automatic schema migrations before steady-state `api` and `worker` startup
- Installation-scoped LiteLLM gateway settings for managed review
- One-way GitHub PR sync from NotebookLens into the native PR surface

## What stays out of scope

- Helm charts, Kubernetes manifests, or cloud-specific installers
- Billing, RBAC, SSO, SCIM, or audit-log export
- Bidirectional GitHub sync back into NotebookLens
- Per-repo AI overrides or a broader managed provider catalog beyond LiteLLM

## Prerequisites

- Docker Engine with the Docker Compose plugin available as `docker compose`
- A DNS name and TLS termination target for `APP_BASE_URL`
- A GitHub App for the repositories you want NotebookLens to review
- GitHub OAuth credentials for reviewer sign-in
- An email provider/API key for notifications

## 1. Prepare the environment file

Start from the checked-in example:

```bash
cp deploy/.env.example deploy/.env
```

Populate every required value in `deploy/.env` before starting the stack.

The most important settings are:

- `APP_BASE_URL`: the only public origin NotebookLens documents and supports
- `DATABASE_URL` and `POSTGRES_PASSWORD`: PostgreSQL connectivity for the managed workspace
- `SESSION_SECRET` and `ENCRYPTION_KEY`: session signing plus encrypted secret storage
- `GITHUB_APP_*`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_OAUTH_*`: GitHub App + GitHub OAuth wiring
- `GITHUB_HOST_KIND`, `GITHUB_API_BASE_URL`, `GITHUB_WEB_BASE_URL`: choose GitHub.com or GHES
- `MANAGED_REVIEW_BETA_ENABLED=true`
- `MANAGED_AI_UI_ENABLED=true`
- `MANAGED_AI_GATEWAY_KIND=litellm`
- `GITHUB_PR_SYNC_ENABLED=true`

## 2. Choose the GitHub host

For GitHub.com, keep the defaults from `deploy/.env.example`:

```dotenv
GITHUB_HOST_KIND=github_com
GITHUB_API_BASE_URL=https://api.github.com
GITHUB_WEB_BASE_URL=https://github.com
```

For GitHub Enterprise Server, set the instance-specific URLs instead:

```dotenv
GITHUB_HOST_KIND=ghes
GITHUB_API_BASE_URL=https://github.internal.example.com/api/v3
GITHUB_WEB_BASE_URL=https://github.internal.example.com
GITHUB_ENTERPRISE_MIN_VERSION=3.20.0
```

## 3. Start the stack

From the repository root:

```bash
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up --build -d
```

Compose starts the following services:

- `gateway`: Caddy reverse proxy for the single public origin
- `web`: Next.js review UI
- `api`: FastAPI routes under `/api/*`
- `worker`: background snapshot and GitHub sync processing
- `postgres`: PostgreSQL state store

## 4. Verify health

Check container status:

```bash
docker compose -f deploy/docker-compose.yml --env-file deploy/.env ps
```

Check the API health endpoint through the public origin:

```bash
curl -f "$APP_BASE_URL/api/healthz"
```

Expected result: an HTTP 200 response once migrations complete and the API is ready.

## 5. Complete the GitHub setup

After the stack is healthy:

1. Install the NotebookLens GitHub App on the target repositories or organization.
2. Point the GitHub App webhook to `$APP_BASE_URL/api/github/webhooks`.
3. Sign in to NotebookLens with GitHub OAuth as a reviewer.
4. Open a pull request with `.ipynb` changes and confirm the `NotebookLens Review Workspace` check run appears.

## 6. Configure managed admin features

The self-hosted operator path and the admin feature path are separate:

- Use [admin-ai-settings.md](admin-ai-settings.md) for installation-scoped LiteLLM setup
- Use [github-pr-sync.md](github-pr-sync.md) for the GitHub mirror contract and fallback behavior

## Day-2 operations

- Use `docker compose ... up --build -d` after pulling new commits to roll forward the stack.
- Compose volumes persist PostgreSQL data plus Caddy state across restarts.
- `SNAPSHOT_RETENTION_DAYS` defaults to `90`; tune it intentionally if operators need a shorter retention window.
- Keep `APP_BASE_URL` stable. The managed UI, API routes, and GitHub links all assume one shared public origin.

## If something does not work

Use [troubleshooting.md](troubleshooting.md) for:

- stack-health checks
- missing check runs
- GitHub OAuth access problems
- LiteLLM fallback behavior
- GitHub PR sync delays or fallback comments

## CI coverage

GitHub Actions validates the managed deployment artifacts by:

- building `apps/api/Dockerfile`
- building `apps/web/Dockerfile`
- rendering `deploy/docker-compose.yml` with `deploy/.env.example`

That CI job is a config/build smoke test. It does not replace an operator smoke deployment with real GitHub credentials.

## Related docs

- [quickstart-workspace.md](quickstart-workspace.md)
- [admin-ai-settings.md](admin-ai-settings.md)
- [github-pr-sync.md](github-pr-sync.md)
- [privacy.md](privacy.md)
- [troubleshooting.md](troubleshooting.md)
