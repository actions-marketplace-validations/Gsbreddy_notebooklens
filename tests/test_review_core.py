from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import pytest

from src import github_action as github_action_module
from src.claude_integration import NoneProvider, ProviderConfig, ProviderInterface
from src.diff_engine import NotebookInput, build_notebook_diff
from src.review_core import (
    REVIEW_SNAPSHOT_SCHEMA_VERSION,
    ReviewArtifacts,
    ReviewCoreRequest,
    build_review_artifacts,
    build_review_snapshot_payload,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures"
SMALL_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO2N1foAAAAASUVORK5CYII="
)


def fixture_text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


class StubGitHubNotebookApi:
    def __init__(self, *, files: Sequence[Mapping[str, Any]], contents: Mapping[tuple[str, str], str]) -> None:
        self.files = [dict(item) for item in files]
        self.contents = dict(contents)

    def list_pull_request_files(self, *, repository: str, pull_number: int) -> Sequence[Any]:
        del repository, pull_number
        return list(self.files)

    def get_file_content(self, *, repository: str, path: str, ref: str) -> Optional[str]:
        del repository
        return self.contents.get((path, ref))


class NoneProviderFactory:
    def __call__(self, config: ProviderConfig) -> ProviderInterface:
        del config
        return NoneProvider()


def _modified_input() -> NotebookInput:
    return NotebookInput(
        path="analysis/notebook.ipynb",
        change_type="modified",
        base_content=fixture_text("simple_base.ipynb"),
        head_content=fixture_text("simple_head.ipynb"),
    )


def _notebook_with_cells(cells: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps(
        {
            "cells": list(cells),
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    )


def _code_cell(
    cell_id: str,
    *,
    outputs: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "cell_type": "code",
        "id": cell_id,
        "source": "render_plot()",
        "metadata": {},
        "outputs": list(outputs or []),
    }


def test_build_review_artifacts_produces_versioned_snapshot_payload() -> None:
    artifacts = build_review_artifacts(
        ReviewCoreRequest(
            notebook_inputs=[_modified_input()],
            reviewer=NoneProvider(),
        )
    )

    assert artifacts.notebook_diff.total_notebooks_changed == 1
    assert artifacts.review_result.summary is None

    payload = artifacts.snapshot_payload
    assert payload["schema_version"] == REVIEW_SNAPSHOT_SCHEMA_VERSION
    notebook = payload["review"]["notebooks"][0]
    assert notebook["path"] == "analysis/notebook.ipynb"
    assert notebook["render_rows"]

    modified_row = next(row for row in notebook["render_rows"] if row["change_type"] == "modified")
    assert modified_row["source"]["changed"] is True
    assert isinstance(modified_row["source"]["base"], str)
    assert isinstance(modified_row["source"]["head"], str)
    assert set(modified_row["thread_anchors"]) == {"source", "outputs", "metadata"}
    assert modified_row["thread_anchors"]["source"]["notebook_path"] == "analysis/notebook.ipynb"
    assert modified_row["thread_anchors"]["source"]["source_fingerprint"]


def test_build_review_artifacts_extracts_supported_image_assets_and_keeps_placeholders() -> None:
    oversized_gif_base64 = base64.b64encode(
        b"GIF89a\x01\x00\x01\x00" + (b"\x00" * 2_097_200)
    ).decode("ascii")
    base_notebook = _notebook_with_cells(
        [
            _code_cell("plot-one"),
            _code_cell("plot-two"),
            _code_cell("svg-plot"),
            _code_cell("too-large-plot"),
        ]
    )
    head_notebook = _notebook_with_cells(
        [
            _code_cell(
                "plot-one",
                outputs=[
                    {
                        "output_type": "display_data",
                        "data": {"image/png": SMALL_PNG_BASE64},
                    }
                ],
            ),
            _code_cell(
                "plot-two",
                outputs=[
                    {
                        "output_type": "display_data",
                        "data": {"image/png": SMALL_PNG_BASE64},
                    }
                ],
            ),
            _code_cell(
                "svg-plot",
                outputs=[
                    {
                        "output_type": "display_data",
                        "data": {
                            "image/svg+xml": (
                                "<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'></svg>"
                            )
                        },
                    }
                ],
            ),
            _code_cell(
                "too-large-plot",
                outputs=[
                    {
                        "output_type": "display_data",
                        "data": {"image/gif": oversized_gif_base64},
                    }
                ],
            ),
        ]
    )

    artifacts = build_review_artifacts(
        ReviewCoreRequest(
            notebook_inputs=[
                NotebookInput(
                    path="analysis/plots.ipynb",
                    change_type="modified",
                    base_content=base_notebook,
                    head_content=head_notebook,
                )
            ],
            reviewer=NoneProvider(),
        )
    )

    assert len(artifacts.review_assets) == 1
    asset = artifacts.review_assets[0]
    assert asset.mime_type == "image/png"
    assert asset.width == 1
    assert asset.height == 1

    rows = {
        row["locator"]["cell_id"]: row
        for row in artifacts.snapshot_payload["review"]["notebooks"][0]["render_rows"]
    }
    plot_one_item = rows["plot-one"]["outputs"]["items"][0]
    plot_two_item = rows["plot-two"]["outputs"]["items"][0]
    assert plot_one_item["kind"] == "image"
    assert plot_one_item["asset_key"] == plot_two_item["asset_key"] == asset.asset_key
    assert plot_one_item["mime_type"] == "image/png"
    assert plot_one_item["width"] == 1
    assert plot_one_item["height"] == 1
    assert plot_one_item["change_type"] == "added"

    svg_item = rows["svg-plot"]["outputs"]["items"][0]
    assert svg_item["kind"] == "placeholder"
    assert "unsupported image format" in svg_item["summary"]
    assert svg_item["change_type"] == "added"

    oversized_item = rows["too-large-plot"]["outputs"]["items"][0]
    assert oversized_item["kind"] == "placeholder"
    assert "exceeds 2097152 bytes" in oversized_item["summary"]
    assert oversized_item["change_type"] == "added"


def test_build_review_snapshot_payload_rejects_unknown_schema_version() -> None:
    diff = build_notebook_diff([_modified_input()])

    with pytest.raises(ValueError, match="Unsupported review snapshot schema version: 2"):
        build_review_snapshot_payload(diff, schema_version=2)


def test_run_action_consumes_shared_review_core_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    api = StubGitHubNotebookApi(
        files=[
            {
                "filename": "analysis/notebook.ipynb",
                "status": "modified",
                "size": 1024,
            }
        ],
        contents={
            ("analysis/notebook.ipynb", "base-sha"): fixture_text("simple_base.ipynb"),
            ("analysis/notebook.ipynb", "head-sha"): fixture_text("simple_head.ipynb"),
        },
    )
    observed: Dict[str, Any] = {}

    def fake_build_review_artifacts(request: ReviewCoreRequest) -> ReviewArtifacts:
        observed["paths"] = [item.path for item in request.notebook_inputs]
        diff = build_notebook_diff(request.notebook_inputs, limits=request.limits)
        review_result = request.reviewer.review(diff)
        return ReviewArtifacts(
            notebook_diff=diff,
            review_result=review_result,
            snapshot_payload={"schema_version": 1, "review": {"notices": [], "notebooks": []}},
            review_assets=[],
        )

    monkeypatch.setattr(github_action_module, "build_review_artifacts", fake_build_review_artifacts)

    result = github_action_module.run_action(
        github_api=api,
        context=github_action_module.PullRequestContext(
            repository="acme/notebooklens-fixture",
            base_repository="acme/notebooklens-fixture",
            head_repository="acme/notebooklens-fixture",
            pull_number=42,
            base_sha="base-sha",
            head_sha="head-sha",
            is_fork=False,
            event_name="pull_request",
            event_action="opened",
        ),
        inputs=github_action_module.ActionInputs(ai_provider="none"),
        provider_factory=NoneProviderFactory(),
        emit_logs=False,
    )

    assert observed["paths"] == ["analysis/notebook.ipynb"]
    assert result.status == "review_ready"
    assert result.notebook_diff is not None
    assert result.review_result is not None
