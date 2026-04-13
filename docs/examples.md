# Examples

Use this page when you want to see what NotebookLens looks like before installing it.

The examples below are static and are written to match the current shipped `v0.4.0-beta` behavior.

## OSS Action Example: Sticky PR Comment

This is the lightweight GitHub-native surface owned by the OSS Action. Reviewers see one sticky PR comment that updates in place on new pushes.

```markdown
## NotebookLens

Reviewed **1** notebook(s) with **4** changed cell(s).

### Notebook Changes

#### `notebooks/training/churn_model.ipynb` (`modified`)
- Changed cells: **4** (added 0, modified 2, deleted 0, moved 1, output-only 1)
- Cells with output updates: **2**
- Cell 2 · `code` · `modified` · cell modified (source) Output updates: text stream output updated (18 chars)
- Cell 4 · `code` · `output_changed` · outputs changed, source unchanged
- Cell 6 · `markdown` · `modified` · cell modified (source)
- Cell 7 · `code` · `moved` · cell reordered without material content changes

##### Reviewer Guidance
- Review the changed outputs and confirm the updated results are intentional.
- Training notebooks: Verify the dataset split and random seed changes are intentional.
- Training notebooks: Check whether metric changes are explained in markdown or the PR description.

- Notebook notices: notebook material metadata changed (kernelspec/language_info)

### Flagged Findings
- **MEDIUM** `notebooks/training/churn_model.ipynb` · Cell 4 · `error` · Changed cell includes an error output. Verify the failing state is intentional. (`error_output_present`, confidence: high)

<details>
<summary>AI summary (Claude)</summary>

The training notebook changes the input dataset and random seed, and the updated output shows lower accuracy than the previous revision. The notebook narrative should explain whether the regression is expected.

</details>
```

What this example demonstrates:

- `none` mode still produces notebook-local reviewer guidance and flagged findings.
- The comment stays notebook-aware instead of dumping raw notebook JSON.
- The Claude summary block appears only when `ai-provider: claude` succeeds.
- The marker comment is updated in place on later pushes and deleted if notebook changes disappear from the PR.

## Hosted Workspace Beta Example: Review Flow

This is the separate hosted surface owned by the NotebookLens GitHub App and web app. In the current `v0.4.0-beta` release it is still **beta**.

### 1. GitHub surface

On a pull request with notebook changes, reviewers see a dedicated check run:

```text
NotebookLens Review Workspace   neutral
Latest snapshot ready · 1 open thread · 0 resolved · Open in NotebookLens
```

The OSS Action comment can still exist on the same PR, but the hosted workspace opens from the check run instead of the sticky comment.

### 2. Hosted review workspace

When the reviewer opens the check run link, the hosted UI shows the latest snapshot:

```text
Review: acme/forecasting #128
Snapshot 2 of 2

Notebook: notebooks/forecast/sales_forecast.ipynb
- changed output plot
- updated markdown explanation
- 1 open thread
```

Inside the workspace, reviewers can:

- switch between snapshot history entries
- inspect notebook-aware source, output, and metadata changes
- create, reply to, resolve, or reopen inline threads on changed blocks

### 3. Thread activity

A reviewer opens a thread on a changed plot block:

```text
Thread: Sales forecast plot changed after the April seasonality update.
Comment: Please explain whether the widened confidence band is expected.
```

If another push updates the notebook and the thread anchor still matches safely, NotebookLens carries the unresolved thread forward to the newest snapshot. If the anchor no longer maps safely, the older thread remains on the earlier snapshot and is marked outdated.

### 4. GitHub PR sync

Hosted thread activity mirrors back into GitHub:

- stable source or markdown anchors become native PR review comments
- unmappable notebook anchors are summarized in the app-owned workspace comment fallback
- user-scoped GitHub credentials are preferred for mirrored reviewer comments, with app-authored fallback when a user token is unavailable

## Which example should you start with?

- Start with the OSS Action example if your team wants the fastest first install.
- Use the hosted workspace example if your team is evaluating the richer review loop in the current beta.
- Use both if you want a sticky PR summary plus a separate hosted review surface on the same pull request.
