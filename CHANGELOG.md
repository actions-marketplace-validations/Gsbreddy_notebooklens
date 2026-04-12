# Changelog

All notable changes to NotebookLens will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0-beta] - 2026-04-13

### Added

- Managed workspace operator docs for the supported Docker Compose self-hosting path, installation-scoped LiteLLM settings, and GitHub PR sync behavior in `v0.4.0-beta`.

### Changed

- CI now validates the managed `apps/api` and `apps/web` Docker image builds and renders `deploy/docker-compose.yml` against `deploy/.env.example`.
- Hosted workspace README scope now reflects `v0.4.0-beta`: Docker Compose self-hosting for internal pilots, installation-scoped LiteLLM configuration, and one-way GitHub PR mirroring while NotebookLens remains the source of truth.

## [0.3.0-beta] - 2026-04-12

### Added

- Hosted PR-linked review workspace beta with notebook-aware diff rendering, snapshot history, and inline thread create/reply/resolve/reopen flows.
- GitHub App onboarding plus GitHub OAuth sign-in for the managed review workspace.
- Dedicated `NotebookLens Review Workspace` check run with snapshot status, hosted-review entrypoint, and thread-count summaries.
- Versioned normalized snapshot storage for PR revisions, including thread carry-forward and `outdated` thread handling when anchors stop matching safely.
- Hosted beta email notifications for thread-created, reply-added, resolved, and reopened events.

### Changed

- Public docs now separate OSS Action onboarding from GitHub App onboarding and clarify that both products can coexist on the same pull request.
- Managed beta scope is now explicitly PR-only and deterministic-local-review-only, with no new public Action `with:` inputs.

## [0.2.0] - 2026-04-12

### Added

- Reviewer guidance playbooks via optional `.github/notebooklens.yml`, merged into notebook-local PR comment guidance sections.
- Built-in reviewer guidance in `none` mode so changed notebooks receive actionable prompts without any external AI call.

### Changed

- Reviewer playbook config is read from the PR head revision, including fork PRs and renamed notebooks matched on their current head path.
- Malformed reviewer-guidance config now falls back to built-in guidance with one visible PR comment notice instead of failing the run.

## [0.1.0] - 2026-04-11

### Added

- Initial public `v0.1.0` GitHub Action release for notebook-aware pull request review.
- Notebook diff engine with cell alignment and support for `added`, `deleted`, `modified`, `output_changed`, and `moved` change types.
- `none` and `claude` review modes with fork-safe Claude fallback behavior.
- Idempotent sticky pull request comments keyed by `<!-- notebooklens-comment -->`.
- Redaction for URI credentials, connection strings, sensitive assignments, long base64 blobs, and optional email addresses.
- Structured stdout logs (`notebooklens.runtime`, `notebooklens.comment_sync`) and action outputs for downstream workflow steps.
- CI workflow that runs `pytest` and a Docker build smoke test.
- Manual release workflow that creates `v0.x.y`, moves the floating `v0` tag, and publishes a GitHub Release from this changelog.
- Contributor guide, security policy, code of conduct, and issue templates.

### Changed

- Docker action image now installs the declared Python dependencies during image build.
- Public docs and examples now reference `Gsbreddy/notebooklens@v0` and document the exact `v0.1.0` pin option.
- Package metadata and runtime version strings are aligned on `0.1.0`.
