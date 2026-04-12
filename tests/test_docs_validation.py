from __future__ import annotations

from pathlib import Path

from scripts.validate_docs import validate_repo_docs


def test_validate_repo_docs_passes() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    assert validate_repo_docs(repo_root) == []


def test_validate_repo_docs_reports_missing_anchor(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    target = docs_dir / "guide.md"

    readme.write_text("[Guide](docs/guide.md#missing-anchor)\n", encoding="utf-8")
    target.write_text("# Real Heading\n", encoding="utf-8")

    errors = validate_repo_docs(tmp_path, source_patterns=("README.md",))

    assert errors == ["README.md:1: missing anchor 'missing-anchor' in 'docs/guide.md'"]
