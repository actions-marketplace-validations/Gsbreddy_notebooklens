from __future__ import annotations

import json
from pathlib import Path

from src.diff_engine import DiffLimits, NotebookInput, build_notebook_diff


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _input(
    *,
    path: str,
    change_type: str,
    base_fixture: str | None = None,
    head_fixture: str | None = None,
    base_size_bytes: int | None = None,
    head_size_bytes: int | None = None,
) -> NotebookInput:
    return NotebookInput(
        path=path,
        change_type=change_type,  # type: ignore[arg-type]
        base_content=fixture_text(base_fixture) if base_fixture else None,
        head_content=fixture_text(head_fixture) if head_fixture else None,
        base_size_bytes=base_size_bytes,
        head_size_bytes=head_size_bytes,
    )


def _notebook_json(cell_count: int, line_prefix: str) -> str:
    cells = []
    for idx in range(cell_count):
        cells.append(
            {
                "cell_type": "code",
                "id": f"c{idx}",
                "metadata": {},
                "execution_count": idx + 1,
                "source": [f"{line_prefix}_{idx}\n"],
                "outputs": [],
            }
        )
    payload = {
        "cells": cells,
        "metadata": {
            "kernelspec": {"name": "python3", "display_name": "Python 3"},
            "language_info": {"name": "python", "version": "3.11"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    return json.dumps(payload)


def test_simple_fixture_detects_modified_and_added_changes() -> None:
    diff = build_notebook_diff(
        [
            _input(
                path="simple.ipynb",
                change_type="modified",
                base_fixture="simple_base.ipynb",
                head_fixture="simple_head.ipynb",
            )
        ]
    )

    notebook = diff.notebooks[0]
    change_types = {item.change_type for item in notebook.cell_changes}
    assert "modified" in change_types
    assert "added" in change_types
    assert diff.total_notebooks_changed == 1
    assert diff.total_cells_changed == len(notebook.cell_changes)
    assert any(item.review_context for item in notebook.cell_changes)


def test_medium_fixture_covers_output_only_metadata_and_notebook_metadata_changes() -> None:
    diff = build_notebook_diff(
        [
            _input(
                path="medium.ipynb",
                change_type="modified",
                base_fixture="medium_base.ipynb",
                head_fixture="medium_head.ipynb",
            )
        ]
    )

    notebook = diff.notebooks[0]
    assert any("notebook material metadata changed" in item for item in notebook.notices)
    assert any(item.change_type == "output_changed" for item in notebook.cell_changes)
    assert any(item.material_metadata_changed for item in notebook.cell_changes)


def test_complex_fixture_covers_moved_added_deleted_and_binary_output_placeholder() -> None:
    diff = build_notebook_diff(
        [
            _input(
                path="complex.ipynb",
                change_type="modified",
                base_fixture="complex_base.ipynb",
                head_fixture="complex_head.ipynb",
            )
        ]
    )

    notebook = diff.notebooks[0]
    change_types = {item.change_type for item in notebook.cell_changes}
    assert "moved" in change_types
    assert "added" in change_types
    assert "deleted" in change_types
    assert "modified" in change_types or "output_changed" in change_types

    image_outputs = [
        output
        for change in notebook.cell_changes
        for output in change.output_changes
        if output.mime_group == "image"
    ]
    assert image_outputs
    assert all("image output updated" in output.summary for output in image_outputs)
    assert all("iVBOR" not in output.summary for output in image_outputs)


def test_metadata_only_fixture_ignores_non_material_churn() -> None:
    diff = build_notebook_diff(
        [
            _input(
                path="metadata_only.ipynb",
                change_type="modified",
                base_fixture="metadata_only_base.ipynb",
                head_fixture="metadata_only_head.ipynb",
            )
        ]
    )

    notebook = diff.notebooks[0]
    assert notebook.cell_changes == []
    assert notebook.notices == []


def test_malformed_fixture_is_skipped_with_parse_notice() -> None:
    diff = build_notebook_diff(
        [
            _input(
                path="broken.ipynb",
                change_type="modified",
                base_fixture="simple_base.ipynb",
                head_fixture="malformed.ipynb",
            )
        ]
    )

    notebook = diff.notebooks[0]
    assert notebook.cell_changes == []
    assert any("failed to parse head notebook JSON" in item for item in notebook.notices)
    assert any("failed to parse head notebook JSON" in item for item in diff.notices)


def test_file_size_limit_skips_large_notebook_and_continues() -> None:
    limits = DiffLimits(max_notebook_bytes=120)
    diff = build_notebook_diff(
        [
            _input(
                path="too_large.ipynb",
                change_type="modified",
                base_fixture="simple_base.ipynb",
                head_fixture="simple_head.ipynb",
                head_size_bytes=121,
            )
        ],
        limits=limits,
    )

    notebook = diff.notebooks[0]
    assert notebook.cell_changes == []
    assert any("skipped notebook larger than 120 bytes" in item for item in diff.notices)


def test_notebook_count_limit_processes_deterministic_subset() -> None:
    inputs = [
        _input(
            path=f"nb_{idx}.ipynb",
            change_type="added",
            head_fixture="simple_head.ipynb",
        )
        for idx in range(22)
    ]
    diff = build_notebook_diff(inputs)

    assert diff.total_notebooks_changed == 20
    assert diff.notebooks[0].path == "nb_0.ipynb"
    assert diff.notebooks[-1].path == "nb_19.ipynb"
    assert any("Processed first 20 notebooks" in item for item in diff.notices)


def test_cell_limit_truncates_after_first_500_aligned_cells() -> None:
    diff = build_notebook_diff(
        [
            NotebookInput(
                path="many_cells.ipynb",
                change_type="modified",
                base_content=_notebook_json(505, "base"),
                head_content=_notebook_json(505, "head"),
            )
        ]
    )

    notebook = diff.notebooks[0]
    assert len(notebook.cell_changes) == 500
    assert any(
        "truncated processing after first 500 aligned cells; skipped 5 cells" in item
        for item in notebook.notices
    )


def test_output_truncation_flag_is_set_for_large_text_outputs() -> None:
    base_payload = {
        "cells": [
            {
                "cell_type": "code",
                "id": "cell-1",
                "metadata": {},
                "execution_count": 1,
                "source": ["print('x')\n"],
                "outputs": [],
            }
        ],
        "metadata": {"kernelspec": {"name": "python3"}, "language_info": {"name": "python"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    head_payload = {
        "cells": [
            {
                "cell_type": "code",
                "id": "cell-1",
                "metadata": {},
                "execution_count": 2,
                "source": ["print('x')\n"],
                "outputs": [
                    {
                        "output_type": "stream",
                        "name": "stdout",
                        "text": "z" * 2200,
                    }
                ],
            }
        ],
        "metadata": {"kernelspec": {"name": "python3"}, "language_info": {"name": "python"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }

    diff = build_notebook_diff(
        [
            NotebookInput(
                path="large_output.ipynb",
                change_type="modified",
                base_content=json.dumps(base_payload),
                head_content=json.dumps(head_payload),
            )
        ]
    )

    output_changes = diff.notebooks[0].cell_changes[0].output_changes
    assert output_changes
    assert output_changes[0].truncated is True
    assert "2200 chars" in output_changes[0].summary
