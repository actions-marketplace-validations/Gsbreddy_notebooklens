from __future__ import annotations

import json
from pathlib import Path

from src.claude_integration import NoneProvider, _redact_text
from src.diff_engine import NotebookInput, build_notebook_diff


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def test_secret_and_email_redaction_masks_sensitive_patterns() -> None:
    sample = fixture_text("secret_notebook.ipynb")
    redacted = _redact_text(sample, redact_secrets=True, redact_emails=True)

    assert "super-secret-token" not in redacted
    assert "hunter2" not in redacted
    assert "sai@example.com" not in redacted
    assert "analyst@example.com" not in redacted
    assert "<REDACTED_SECRET>" in redacted
    assert "<REDACTED_EMAIL>" in redacted
    assert "postgresql://alice:pw123@db.internal:5432/app" not in redacted
    assert "<REDACTED_BASE64_BLOB>" in redacted


def test_redaction_flags_can_be_disabled() -> None:
    sample = "API_KEY=token123 email=user@example.com"
    not_redacted = _redact_text(sample, redact_secrets=False, redact_emails=False)
    assert not_redacted == sample


def test_none_provider_emits_objective_findings_for_metadata_error_and_large_output() -> None:
    medium_input = NotebookInput(
        path="medium.ipynb",
        change_type="modified",
        base_content=fixture_text("medium_base.ipynb"),
        head_content=fixture_text("medium_head.ipynb"),
    )

    base_payload = {
        "cells": [
            {
                "cell_type": "code",
                "id": "run-cell",
                "metadata": {},
                "execution_count": 1,
                "source": ["run()\n"],
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
                "id": "run-cell",
                "metadata": {},
                "execution_count": 2,
                "source": ["run()\n"],
                "outputs": [
                    {
                        "output_type": "stream",
                        "name": "stdout",
                        "text": "x" * 2100,
                    },
                    {
                        "output_type": "error",
                        "ename": "RuntimeError",
                        "evalue": "boom",
                        "traceback": ["RuntimeError: boom"],
                    },
                ],
            }
        ],
        "metadata": {"kernelspec": {"name": "python3"}, "language_info": {"name": "python"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    error_and_large_output_input = NotebookInput(
        path="runtime.ipynb",
        change_type="modified",
        base_content=json.dumps(base_payload),
        head_content=json.dumps(head_payload),
    )

    diff = build_notebook_diff([medium_input, error_and_large_output_input])
    result = NoneProvider().review(diff)
    codes = {issue.code for issue in result.flagged_issues}

    assert "notebook_material_metadata_changed" in codes
    assert "cell_material_metadata_changed" in codes
    assert "error_output_present" in codes
    assert "large_output_change" in codes
