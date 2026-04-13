# Contributing to NotebookLens

Thanks for helping improve NotebookLens.

## Local Development

### Prerequisites

- Python 3.9+
- Git
- Docker

### Setup

```bash
git clone https://github.com/Gsbreddy/notebooklens.git
cd notebooklens

python -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## Validation

Run the full local validation suite before opening a pull request:

```bash
python -m mkdocs build --strict
pytest
python3 -m py_compile src/*.py
docker build -t notebooklens-local .
git diff --check
```

The published documentation site is deployed from `main` through the `Docs Pages` workflow and serves the MkDocs build from `mkdocs.yml`.

## Repository Layout

- `src/diff_engine.py` implements notebook parsing, alignment, and change classification.
- `src/claude_integration.py` implements provider logic, redaction, and strict Claude response handling.
- `src/github_api.py` handles GitHub API access, PR comment rendering, and sticky comment sync.
- `src/github_action.py` is the Docker action runtime entrypoint and output/logging layer.
- `tests/` contains integration tests plus notebook fixtures.

## Pull Requests

1. Branch from `main`.
2. Keep behavior changes covered by tests.
3. Update README or changelog when public behavior changes.
4. Make sure the `pytest` GitHub Actions job passes before requesting review.

## Maintainer Release Process

1. Merge the release-ready branch into `main`.
2. Open the Actions tab and run the `Release` workflow on `main`.
3. Provide a version input in `0.x.y` form, for example `0.4.0`.
4. Verify:
   - tag `v0.x.y` exists
   - floating tag `v0` points to the same commit
   - GitHub Release notes match `CHANGELOG.md`

The current release workflow accepts only `0.x.y` version inputs. Changelog or docs references to an upcoming beta line such as `v0.4.1-beta` are planning markers until a matching prerelease tagging flow is added.

## Community Standards

- Read [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) before participating.
- Use [SECURITY.md](SECURITY.md) for vulnerability reporting guidance.
