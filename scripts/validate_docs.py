#!/usr/bin/env python3
"""Validate NotebookLens README/docs links and lightweight docs consistency."""

from __future__ import annotations

import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


REPO_DOC_SOURCES = ("README.md", "docs/*.md")
IGNORED_LINK_PREFIXES = ("http://", "https://", "mailto:", "tel:")
PLACEHOLDER_STRINGS = ("your-org/notebooklens",)
MARKDOWN_LINK_RE = re.compile(r"(?P<image>!?)\[[^\]]+\]\((?P<target>[^)]+)\)")


@dataclass(frozen=True)
class LinkRef:
    source: Path
    line_number: int
    target: str


def _iter_doc_sources(repo_root: Path, source_patterns: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for pattern in source_patterns:
        if "*" in pattern:
            files.extend(sorted(repo_root.glob(pattern)))
            continue
        files.append(repo_root / pattern)
    unique_files = []
    seen: set[Path] = set()
    for path in files:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_files.append(path)
    return unique_files


def _iter_non_fenced_lines(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    in_fence = False
    for index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        lines.append((index, line))
    return lines


def _github_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.strip().lower()
    lowered = re.sub(r"[`*_~]", "", lowered)
    lowered = re.sub(r"[^a-z0-9 -]", "", lowered)
    slug = re.sub(r"\s+", "-", lowered)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug


def _collect_markdown_anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    slug_counts: Counter[str] = Counter()
    text = path.read_text(encoding="utf-8")
    for _, line in _iter_non_fenced_lines(text):
        if not line.startswith("#"):
            continue
        heading = line.lstrip("#").strip()
        if not heading:
            continue
        slug = _github_slug(heading)
        if not slug:
            continue
        duplicate_index = slug_counts[slug]
        anchor = slug if duplicate_index == 0 else f"{slug}-{duplicate_index}"
        anchors.add(anchor)
        slug_counts[slug] += 1
    return anchors


def _extract_links(path: Path) -> list[LinkRef]:
    refs: list[LinkRef] = []
    text = path.read_text(encoding="utf-8")
    for line_number, line in _iter_non_fenced_lines(text):
        for match in MARKDOWN_LINK_RE.finditer(line):
            target = match.group("target").strip()
            if not target:
                continue
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1].strip()
            refs.append(LinkRef(source=path, line_number=line_number, target=target))
    return refs


def validate_repo_docs(
    repo_root: Path,
    source_patterns: tuple[str, ...] = REPO_DOC_SOURCES,
) -> list[str]:
    errors: list[str] = []
    sources = _iter_doc_sources(repo_root, source_patterns)

    for path in sources:
        if not path.exists():
            errors.append(f"{path.relative_to(repo_root)}: missing source file")
            continue

        text = path.read_text(encoding="utf-8")
        for placeholder in PLACEHOLDER_STRINGS:
            if placeholder in text:
                errors.append(
                    f"{path.relative_to(repo_root)}: contains placeholder '{placeholder}'"
                )

        for ref in _extract_links(path):
            target = ref.target
            if target.startswith(IGNORED_LINK_PREFIXES):
                continue

            path_part, _, anchor = target.partition("#")
            if path_part:
                resolved = (ref.source.parent / path_part).resolve()
            else:
                resolved = ref.source.resolve()

            if not resolved.exists():
                errors.append(
                    f"{ref.source.relative_to(repo_root)}:{ref.line_number}: "
                    f"missing link target '{target}'"
                )
                continue

            if anchor:
                if resolved.suffix.lower() != ".md":
                    errors.append(
                        f"{ref.source.relative_to(repo_root)}:{ref.line_number}: "
                        f"anchor target '{target}' is not a markdown file"
                    )
                    continue
                anchors = _collect_markdown_anchors(resolved)
                if anchor not in anchors:
                    errors.append(
                        f"{ref.source.relative_to(repo_root)}:{ref.line_number}: "
                        f"missing anchor '{anchor}' in '{Path(path_part or ref.source.name)}'"
                    )

    return errors


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    errors = validate_repo_docs(repo_root)
    if not errors:
        print("Docs validation passed for README.md and docs/*.md.")
        return 0

    print("Docs validation failed:")
    for error in errors:
        print(f"- {error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
