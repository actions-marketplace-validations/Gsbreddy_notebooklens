from __future__ import annotations

from src.claude_integration import _merge_reviewer_guidance, _playbook_matches_path
from src.diff_engine import ReviewerGuidanceItem
from src.github_action import ReviewerPlaybookConfig, _parse_notebooklens_config


def test_parse_notebooklens_config_normalizes_and_dedupes_playbooks() -> None:
    content = """
version: 1
reviewer_guidance:
  playbooks:
    - name: "  Training   notebooks  "
      paths:
        - notebooks\\training\\**\\*.ipynb
        - notebooks/training/**/*.ipynb
      prompts:
        - " Verify the dataset split. "
        - "Verify the dataset split."
        - "Check whether metric changes are explained."
"""

    config, notices = _parse_notebooklens_config(content)

    assert notices == []
    assert config is not None
    assert config.version == 1
    assert len(config.reviewer_playbooks) == 1
    playbook = config.reviewer_playbooks[0]
    assert playbook.name == "Training notebooks"
    assert playbook.paths == ("notebooks/training/**/*.ipynb",)
    assert playbook.prompts == (
        "Verify the dataset split.",
        "Check whether metric changes are explained.",
    )


def test_parse_notebooklens_config_invalid_schema_warns_and_returns_none() -> None:
    config, notices = _parse_notebooklens_config(
        """
version: 1
reviewer_guidance:
  playbooks:
    - name: Training notebooks
      paths: []
      prompts:
        - Verify seeds.
"""
    )

    assert config is None
    assert len(notices) == 1
    assert ".github/notebooklens.yml" in notices[0]
    assert "reviewer_guidance.playbooks[0].paths" in notices[0]
    assert "Continuing with built-in guidance only." in notices[0]


def test_playbook_path_glob_matching_handles_nested_paths() -> None:
    playbook = ReviewerPlaybookConfig(
        name="Training notebooks",
        paths=("notebooks/training/**/*.ipynb",),
        prompts=("Verify seeds.",),
    )

    assert _playbook_matches_path(playbook, "notebooks/training/churn_model.ipynb") is True
    assert _playbook_matches_path(playbook, "notebooks/training/models/churn_model.ipynb") is True
    assert _playbook_matches_path(playbook, "notebooks/eval/churn_model.ipynb") is False


def test_merge_reviewer_guidance_dedupes_per_notebook_and_sorts_by_priority_source() -> None:
    merged = _merge_reviewer_guidance(
        [
            ReviewerGuidanceItem(
                notebook_path="analysis.ipynb",
                locator=None,
                code="built_in:material_output_changes",
                source="built_in",
                label=None,
                priority="medium",
                message="Inspect changed outputs.",
            ),
            ReviewerGuidanceItem(
                notebook_path="analysis.ipynb",
                locator=None,
                code="built_in:introduced_error_outputs",
                source="built_in",
                label=None,
                priority="high",
                message="Review introduced error outputs.",
            ),
        ],
        [
            ReviewerGuidanceItem(
                notebook_path="analysis.ipynb",
                locator=None,
                code="claude:duplicate_outputs_hint",
                source="claude",
                label="AI guidance",
                priority="low",
                message="  inspect changed outputs.  ",
            ),
            ReviewerGuidanceItem(
                notebook_path="analysis.ipynb",
                locator=None,
                code="playbook:training_notebooks",
                source="playbook",
                label="Training notebooks",
                priority="medium",
                message="Check whether metric changes are explained.",
            ),
            ReviewerGuidanceItem(
                notebook_path="other.ipynb",
                locator=None,
                code="claude:cross_notebook_repeat",
                source="claude",
                label="AI guidance",
                priority="low",
                message="Inspect changed outputs.",
            ),
        ],
    )

    assert [(item.notebook_path, item.message) for item in merged] == [
        ("analysis.ipynb", "Review introduced error outputs."),
        ("analysis.ipynb", "Inspect changed outputs."),
        ("analysis.ipynb", "Check whether metric changes are explained."),
        ("other.ipynb", "Inspect changed outputs."),
    ]
    assert [item.source for item in merged] == [
        "built_in",
        "built_in",
        "playbook",
        "claude",
    ]
