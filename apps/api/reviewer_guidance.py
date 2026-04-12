"""Managed reviewer-guidance parsing and matching for snapshot builds."""

from __future__ import annotations

from dataclasses import dataclass
import glob
import re
from typing import Iterable, Sequence

from src.diff_engine import CellChange, NotebookDiff, NotebookFileDiff


class NotebookLensConfigError(ValueError):
    """Raised when `.github/notebooklens.yml` is malformed."""


@dataclass(frozen=True)
class ReviewerPlaybook:
    """Validated reviewer playbook from `.github/notebooklens.yml`."""

    name: str
    paths: tuple[str, ...]
    prompts: tuple[str, ...]


_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_SOURCE_ORDER = {"built_in": 0, "playbook": 1}


def parse_reviewer_playbooks(config_text: str) -> tuple[ReviewerPlaybook, ...]:
    """Parse the limited playbook subset supported by the managed snapshot worker."""
    lines = _tokenize_config(config_text)
    version: int | None = None
    playbooks: list[ReviewerPlaybook] = []
    index = 0

    while index < len(lines):
        indent, content = lines[index]
        if indent == 0 and content.startswith("version:"):
            raw_version = _parse_scalar(content.partition(":")[2])
            if raw_version != 1:
                raise NotebookLensConfigError("`.github/notebooklens.yml` must declare `version: 1`")
            version = 1
            index += 1
            continue

        if indent == 0 and content == "reviewer_guidance:":
            index += 1
            if index >= len(lines):
                break
            while index < len(lines):
                nested_indent, nested_content = lines[index]
                if nested_indent <= indent:
                    break
                if nested_indent == 2 and nested_content == "playbooks:":
                    parsed, index = _parse_playbooks(lines, index + 1)
                    playbooks.extend(parsed)
                    continue
                raise NotebookLensConfigError(
                    f"Unsupported config key under `reviewer_guidance`: {nested_content}"
                )
            continue

        raise NotebookLensConfigError(f"Unsupported config entry: {content}")

    if version != 1:
        raise NotebookLensConfigError("`.github/notebooklens.yml` must declare `version: 1`")
    return tuple(playbooks)


def build_reviewer_guidance(
    notebook_diff: NotebookDiff,
    *,
    playbooks: Sequence[ReviewerPlaybook] = (),
) -> list[dict]:
    """Build managed reviewer guidance from deterministic rules and matching playbooks."""
    items: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for notebook in notebook_diff.notebooks:
        for item in _built_in_guidance(notebook):
            dedupe_key = (item["notebook_path"], _normalize_message(item["message"]))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            items.append(item)

        for playbook in playbooks:
            if not any(_path_matches(notebook.path, pattern) for pattern in playbook.paths):
                continue
            for prompt in playbook.prompts:
                item = {
                    "notebook_path": notebook.path,
                    "locator": None,
                    "code": f"playbook:{_normalize_code_fragment(playbook.name)}",
                    "source": "playbook",
                    "label": playbook.name,
                    "priority": "medium",
                    "message": prompt,
                }
                dedupe_key = (item["notebook_path"], _normalize_message(item["message"]))
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                items.append(item)

    items.sort(
        key=lambda item: (
            _PRIORITY_ORDER.get(str(item.get("priority")), 99),
            _SOURCE_ORDER.get(str(item.get("source")), 99),
            str(item.get("notebook_path")),
            str(item.get("code")),
            str(item.get("message")),
        )
    )
    return items


def _built_in_guidance(notebook: NotebookFileDiff) -> Iterable[dict]:
    output_cell = _first_cell(
        notebook,
        lambda cell: cell.outputs_changed or cell.change_type == "output_changed",
    )
    if output_cell is not None:
        yield _guidance_item(
            notebook=notebook,
            cell=output_cell,
            code="built_in:outputs_changed",
            priority="high",
            message=(
                "Inspect changed outputs and confirm the notebook narrative still matches "
                "the latest execution results."
            ),
        )

    metadata_cell = _first_cell(notebook, lambda cell: cell.material_metadata_changed)
    if metadata_cell is not None or any(
        "metadata changed" in notice.lower() for notice in notebook.notices
    ):
        yield _guidance_item(
            notebook=notebook,
            cell=metadata_cell,
            code="built_in:metadata_changed",
            priority="medium",
            message=(
                "Review metadata changes for execution or rendering impact before approving."
            ),
        )

    flow_cell = _first_cell(
        notebook,
        lambda cell: cell.cell_type == "code" and cell.change_type in {"moved", "deleted"},
    )
    if flow_cell is not None:
        yield _guidance_item(
            notebook=notebook,
            cell=flow_cell,
            code="built_in:execution_flow_changed",
            priority="high",
            message=(
                "Review moved or deleted code cells for execution-order or narrative regressions."
            ),
        )


def _guidance_item(
    *,
    notebook: NotebookFileDiff,
    cell: CellChange | None,
    code: str,
    priority: str,
    message: str,
) -> dict:
    locator = None
    if cell is not None:
        locator = {
            "cell_id": cell.locator.cell_id,
            "base_index": cell.locator.base_index,
            "head_index": cell.locator.head_index,
            "display_index": cell.locator.display_index,
        }
    return {
        "notebook_path": notebook.path,
        "locator": locator,
        "code": code,
        "source": "built_in",
        "label": None,
        "priority": priority,
        "message": message,
    }


def _first_cell(notebook: NotebookFileDiff, predicate) -> CellChange | None:
    for cell in notebook.cell_changes:
        if predicate(cell):
            return cell
    return None


def _path_matches(path: str, pattern: str) -> bool:
    regex = re.compile(glob.translate(pattern, recursive=True, include_hidden=True))
    return bool(regex.match(path))


def _normalize_code_fragment(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_") or "unnamed"


def _normalize_message(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _parse_playbooks(
    lines: Sequence[tuple[int, str]],
    index: int,
) -> tuple[list[ReviewerPlaybook], int]:
    playbooks: list[ReviewerPlaybook] = []
    while index < len(lines):
        indent, content = lines[index]
        if indent < 4:
            break
        if indent != 4 or not content.startswith("- "):
            raise NotebookLensConfigError("Playbooks must use `- name:` list entries")

        head = content[2:]
        if not head.startswith("name:"):
            raise NotebookLensConfigError("Each playbook must start with `name:`")
        name = str(_parse_scalar(head.partition(":")[2])).strip()
        index += 1

        paths: list[str] | None = None
        prompts: list[str] | None = None
        while index < len(lines):
            nested_indent, nested_content = lines[index]
            if nested_indent <= 4:
                break
            if nested_indent == 6 and nested_content == "paths:":
                paths, index = _parse_string_list(lines, index + 1, expected_indent=8)
                continue
            if nested_indent == 6 and nested_content == "prompts:":
                prompts, index = _parse_string_list(lines, index + 1, expected_indent=8)
                continue
            raise NotebookLensConfigError(
                f"Unsupported playbook field in `{name}`: {nested_content}"
            )

        if not name or not paths or not prompts:
            raise NotebookLensConfigError(
                "Each playbook requires non-empty `name`, `paths`, and `prompts`"
            )
        playbooks.append(
            ReviewerPlaybook(
                name=name,
                paths=tuple(paths),
                prompts=tuple(prompts),
            )
        )
    return playbooks, index


def _parse_string_list(
    lines: Sequence[tuple[int, str]],
    index: int,
    *,
    expected_indent: int,
) -> tuple[list[str], int]:
    values: list[str] = []
    while index < len(lines):
        indent, content = lines[index]
        if indent < expected_indent:
            break
        if indent != expected_indent or not content.startswith("- "):
            raise NotebookLensConfigError("List values must use `- value` entries")
        parsed = _parse_scalar(content[2:])
        if not isinstance(parsed, str) or not parsed.strip():
            raise NotebookLensConfigError("List values must be non-empty strings")
        values.append(parsed.strip())
        index += 1
    if not values:
        raise NotebookLensConfigError("Expected at least one list value")
    return values, index


def _tokenize_config(config_text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for raw_line in config_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "\t" in raw_line:
            raise NotebookLensConfigError("Tabs are not supported in `.github/notebooklens.yml`")
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        lines.append((indent, stripped))
    return lines


def _parse_scalar(raw_value: str) -> object:
    value = raw_value.strip()
    if not value:
        return ""
    if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
        return value[1:-1]
    if value.isdigit():
        return int(value)
    return value


__all__ = [
    "NotebookLensConfigError",
    "ReviewerPlaybook",
    "build_reviewer_guidance",
    "parse_reviewer_playbooks",
]
