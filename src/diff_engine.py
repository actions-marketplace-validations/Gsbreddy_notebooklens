"""Notebook diff engine for NotebookLens."""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
import json
import re
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Tuple


ChangeType = Literal["added", "modified", "deleted"]
CellType = Literal["code", "markdown", "raw"]
CellChangeType = Literal["added", "modified", "deleted", "moved", "output_changed"]
OutputType = Literal["stream", "error", "display_data", "execute_result"]
MimeGroup = Literal["text", "html", "json", "image", "table", "unknown"]
RelativePosition = Literal["before", "after"]
Severity = Literal["low", "medium", "high"]
IssueCategory = Literal[
    "documentation",
    "output",
    "error",
    "data",
    "metadata",
    "policy",
    "review_guidance",
]
Confidence = Literal["low", "medium", "high"]
ReviewerGuidanceSource = Literal["built_in", "playbook", "claude"]
ReviewerGuidancePriority = Literal["low", "medium", "high"]


MAX_NOTEBOOK_BYTES = 50 * 1024 * 1024
MAX_CELLS_PER_NOTEBOOK = 500
MAX_NOTEBOOKS_PER_PR = 20
MAX_OUTPUT_TEXT_FOR_AI_CHARS = 2_000

VALID_CELL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class CellLocator:
    cell_id: Optional[str]
    base_index: Optional[int]
    head_index: Optional[int]
    display_index: Optional[int]


@dataclass(frozen=True)
class OutputChange:
    output_type: OutputType
    mime_group: MimeGroup
    summary: str
    truncated: bool


@dataclass(frozen=True)
class ContextCell:
    relative_position: RelativePosition
    cell_type: CellType
    summary: str


@dataclass(frozen=True)
class CellChange:
    locator: CellLocator
    cell_type: CellType
    change_type: CellChangeType
    summary: str
    base_source: Optional[str]
    head_source: Optional[str]
    source_changed: bool
    outputs_changed: bool
    material_metadata_changed: bool
    metadata_summary: Optional[str]
    output_changes: List[OutputChange]
    review_context: List[ContextCell]


@dataclass(frozen=True)
class NotebookFileDiff:
    path: str
    change_type: ChangeType
    cell_changes: List[CellChange]
    notices: List[str]


@dataclass(frozen=True)
class NotebookDiff:
    notebooks: List[NotebookFileDiff]
    total_notebooks_changed: int
    total_cells_changed: int
    notices: List[str]


@dataclass(frozen=True)
class FlaggedIssue:
    notebook_path: str
    locator: CellLocator
    code: str
    category: IssueCategory
    severity: Severity
    confidence: Optional[Confidence]
    message: str


@dataclass(frozen=True)
class ReviewerGuidanceItem:
    notebook_path: str
    locator: Optional[CellLocator]
    code: str
    source: ReviewerGuidanceSource
    label: Optional[str]
    priority: ReviewerGuidancePriority
    message: str


@dataclass(frozen=True)
class ReviewResult:
    summary: Optional[str]
    flagged_issues: List[FlaggedIssue]
    reviewer_guidance: List[ReviewerGuidanceItem] = field(default_factory=list)


@dataclass(frozen=True)
class NotebookInput:
    path: str
    change_type: ChangeType
    base_content: Optional[str] = None
    head_content: Optional[str] = None
    base_size_bytes: Optional[int] = None
    head_size_bytes: Optional[int] = None


@dataclass(frozen=True)
class DiffLimits:
    max_notebook_bytes: int = MAX_NOTEBOOK_BYTES
    max_cells_per_notebook: int = MAX_CELLS_PER_NOTEBOOK
    max_notebooks_per_pr: int = MAX_NOTEBOOKS_PER_PR
    max_output_text_for_ai_chars: int = MAX_OUTPUT_TEXT_FOR_AI_CHARS


@dataclass(frozen=True)
class _ParsedNotebook:
    cells: List["_Cell"]
    material_metadata: Dict[str, Any]


@dataclass(frozen=True)
class _Cell:
    cell_type: CellType
    source: str
    outputs: List[Dict[str, Any]]
    material_metadata: Dict[str, Any]
    cell_id: Optional[str]
    index: int


@dataclass(frozen=True)
class _AlignmentRow:
    base_index: Optional[int]
    head_index: Optional[int]
    matched_by: Literal["cell_id", "sequence", "position", "unmatched"]


@dataclass(frozen=True)
class _PairDiff:
    base_index: Optional[int]
    head_index: Optional[int]
    cell_type: CellType
    base_source: Optional[str]
    head_source: Optional[str]
    source_changed: bool
    outputs_changed: bool
    material_metadata_changed: bool
    output_changes: List[OutputChange]


def build_notebook_diff(
    notebook_inputs: Sequence[NotebookInput],
    *,
    limits: DiffLimits = DiffLimits(),
) -> NotebookDiff:
    """Build provider-agnostic notebook diffs with deterministic truncation."""
    notices: List[str] = []
    notebooks: List[NotebookFileDiff] = []

    selected_inputs = list(notebook_inputs[: limits.max_notebooks_per_pr])
    if len(notebook_inputs) > limits.max_notebooks_per_pr:
        notices.append(
            (
                f"Processed first {limits.max_notebooks_per_pr} notebooks in changed-file order; "
                f"skipped {len(notebook_inputs) - limits.max_notebooks_per_pr} additional notebooks."
            )
        )

    for notebook_input in selected_inputs:
        notebook_diff, notebook_notices = _diff_single_notebook(notebook_input, limits=limits)
        notebooks.append(notebook_diff)
        notices.extend(notebook_notices)

    total_cells_changed = sum(len(notebook.cell_changes) for notebook in notebooks)
    return NotebookDiff(
        notebooks=notebooks,
        total_notebooks_changed=len(notebooks),
        total_cells_changed=total_cells_changed,
        notices=notices,
    )


def _diff_single_notebook(
    notebook_input: NotebookInput,
    *,
    limits: DiffLimits,
) -> Tuple[NotebookFileDiff, List[str]]:
    notices: List[str] = []
    global_notices: List[str] = []

    effective_size = max(
        _effective_size(notebook_input.base_content, notebook_input.base_size_bytes),
        _effective_size(notebook_input.head_content, notebook_input.head_size_bytes),
    )
    if effective_size > limits.max_notebook_bytes:
        message = (
            f"{notebook_input.path}: skipped notebook larger than {limits.max_notebook_bytes} bytes."
        )
        notices.append(message)
        global_notices.append(message)
        return (
            NotebookFileDiff(
                path=notebook_input.path,
                change_type=notebook_input.change_type,
                cell_changes=[],
                notices=notices,
            ),
            global_notices,
        )

    base_nb: Optional[_ParsedNotebook]
    head_nb: Optional[_ParsedNotebook]

    if notebook_input.change_type == "added":
        base_nb = _ParsedNotebook(cells=[], material_metadata={})
        head_parse = _parse_notebook(notebook_input.head_content)
        head_nb = head_parse[0]
        if head_parse[1] is not None:
            message = f"{notebook_input.path}: failed to parse head notebook JSON ({head_parse[1]})."
            notices.append(message)
            global_notices.append(message)
    elif notebook_input.change_type == "deleted":
        head_nb = _ParsedNotebook(cells=[], material_metadata={})
        base_parse = _parse_notebook(notebook_input.base_content)
        base_nb = base_parse[0]
        if base_parse[1] is not None:
            message = f"{notebook_input.path}: failed to parse base notebook JSON ({base_parse[1]})."
            notices.append(message)
            global_notices.append(message)
    else:
        base_parse = _parse_notebook(notebook_input.base_content)
        head_parse = _parse_notebook(notebook_input.head_content)
        base_nb = base_parse[0]
        head_nb = head_parse[0]
        if base_parse[1] is not None:
            message = f"{notebook_input.path}: failed to parse base notebook JSON ({base_parse[1]})."
            notices.append(message)
            global_notices.append(message)
        if head_parse[1] is not None:
            message = f"{notebook_input.path}: failed to parse head notebook JSON ({head_parse[1]})."
            notices.append(message)
            global_notices.append(message)

    if base_nb is None or head_nb is None:
        return (
            NotebookFileDiff(
                path=notebook_input.path,
                change_type=notebook_input.change_type,
                cell_changes=[],
                notices=notices,
            ),
            global_notices,
        )

    notebook_metadata_notice = _summarize_notebook_metadata_change(base_nb, head_nb)
    if notebook_metadata_notice is not None:
        notices.append(notebook_metadata_notice)

    alignment_rows = _align_cells(base_nb.cells, head_nb.cells)
    if len(alignment_rows) > limits.max_cells_per_notebook:
        skipped = len(alignment_rows) - limits.max_cells_per_notebook
        notices.append(
            (
                f"{notebook_input.path}: truncated processing after first "
                f"{limits.max_cells_per_notebook} aligned cells; skipped {skipped} cells."
            )
        )
        alignment_rows = alignment_rows[: limits.max_cells_per_notebook]

    pair_diffs = _build_pair_diffs(base_nb.cells, head_nb.cells, alignment_rows, limits=limits)
    moved_pairs = _detect_moved_pairs(pair_diffs)

    cell_changes: List[CellChange] = []
    for pair in pair_diffs:
        change_type = _classify_cell_change(pair, moved_pairs)
        if change_type is None:
            continue

        locator = CellLocator(
            cell_id=_pick_cell_id(base_nb.cells, head_nb.cells, pair.base_index, pair.head_index),
            base_index=pair.base_index,
            head_index=pair.head_index,
            display_index=_display_index(pair),
        )
        reference_cells = head_nb.cells if pair.head_index is not None else base_nb.cells
        reference_index = pair.head_index if pair.head_index is not None else pair.base_index
        review_context = _build_review_context(reference_cells, reference_index)
        summary = _cell_summary(change_type, pair)

        cell_changes.append(
            CellChange(
                locator=locator,
                cell_type=pair.cell_type,
                change_type=change_type,
                summary=summary,
                base_source=pair.base_source,
                head_source=pair.head_source,
                source_changed=pair.source_changed,
                outputs_changed=pair.outputs_changed,
                material_metadata_changed=pair.material_metadata_changed,
                metadata_summary=_metadata_summary(pair.material_metadata_changed),
                output_changes=pair.output_changes,
                review_context=review_context,
            )
        )

    return (
        NotebookFileDiff(
            path=notebook_input.path,
            change_type=notebook_input.change_type,
            cell_changes=cell_changes,
            notices=notices,
        ),
        global_notices,
    )


def _effective_size(content: Optional[str], declared_size: Optional[int]) -> int:
    if declared_size is not None:
        return declared_size
    if content is None:
        return 0
    return len(content.encode("utf-8"))


def _parse_notebook(content: Optional[str]) -> Tuple[Optional[_ParsedNotebook], Optional[str]]:
    if content is None:
        return None, "missing content"

    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON at line {exc.lineno} col {exc.colno}"

    if not isinstance(payload, dict):
        return None, "top-level JSON value is not an object"
    raw_cells = payload.get("cells")
    if not isinstance(raw_cells, list):
        return None, "notebook does not contain a valid cells array"

    cells: List[_Cell] = []
    for raw_cell in raw_cells:
        if not isinstance(raw_cell, dict):
            continue
        index = len(cells)
        cell_type = _coerce_cell_type(raw_cell.get("cell_type"))
        source = _normalize_source(raw_cell.get("source"))
        outputs = _normalize_outputs(raw_cell.get("outputs"))
        material_metadata = _material_cell_metadata(raw_cell.get("metadata"))
        cell_id = _normalize_cell_id(raw_cell.get("id"))
        cells.append(
            _Cell(
                cell_type=cell_type,
                source=source,
                outputs=outputs,
                material_metadata=material_metadata,
                cell_id=cell_id,
                index=index,
            )
        )

    return (
        _ParsedNotebook(
            cells=cells,
            material_metadata=_material_notebook_metadata(payload.get("metadata")),
        ),
        None,
    )


def _coerce_cell_type(value: Any) -> CellType:
    if value in {"code", "markdown", "raw"}:
        return value
    return "raw"


def _normalize_source(source: Any) -> str:
    if isinstance(source, str):
        return source
    if isinstance(source, list):
        return "".join(part for part in source if isinstance(part, str))
    return ""


def _normalize_outputs(outputs: Any) -> List[Dict[str, Any]]:
    if not isinstance(outputs, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for output in outputs:
        if isinstance(output, dict):
            normalized.append(_normalize_output(output))
    return normalized


def _normalize_output(output: Dict[str, Any]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    for key, value in output.items():
        if key in {"execution_count", "metadata"}:
            # Ignore volatile output metadata churn for review significance.
            continue
        normalized[str(key)] = _stable_jsonable(value)
    return normalized


def _normalize_cell_id(cell_id: Any) -> Optional[str]:
    if isinstance(cell_id, str):
        stripped = cell_id.strip()
        if stripped:
            return stripped
    return None


def _material_notebook_metadata(metadata: Any) -> Dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}

    kernelspec = metadata.get("kernelspec")
    language_info = metadata.get("language_info")
    material: Dict[str, Any] = {}

    if isinstance(kernelspec, dict):
        material["kernelspec"] = _stable_jsonable(kernelspec)
    if isinstance(language_info, dict):
        allowed_language = {}
        if "name" in language_info:
            allowed_language["name"] = language_info.get("name")
        if "version" in language_info:
            allowed_language["version"] = language_info.get("version")
        if allowed_language:
            material["language_info"] = _stable_jsonable(allowed_language)
    return material


def _material_cell_metadata(metadata: Any) -> Dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}

    tags = metadata.get("tags")
    if isinstance(tags, list):
        normalized_tags = sorted(str(tag) for tag in tags)
        return {"tags": normalized_tags}
    return {}


def _stable_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _stable_jsonable(val) for key, val in sorted(value.items())}
    if isinstance(value, list):
        return [_stable_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _align_cells(base_cells: Sequence[_Cell], head_cells: Sequence[_Cell]) -> List[_AlignmentRow]:
    matched: Dict[int, Tuple[int, Literal["cell_id", "sequence", "position"]]] = {}
    used_head: set[int] = set()

    for base_index, head_index in _match_by_cell_id(base_cells, head_cells):
        matched[base_index] = (head_index, "cell_id")
        used_head.add(head_index)

    unmatched_base = [idx for idx in range(len(base_cells)) if idx not in matched]
    unmatched_head = [idx for idx in range(len(head_cells)) if idx not in used_head]

    for base_index, head_index in _match_by_sequence(base_cells, head_cells, unmatched_base, unmatched_head):
        matched[base_index] = (head_index, "sequence")
        used_head.add(head_index)

    unmatched_base = [idx for idx in range(len(base_cells)) if idx not in matched]
    unmatched_head = [idx for idx in range(len(head_cells)) if idx not in used_head]

    for base_index, head_index in zip(unmatched_base, unmatched_head):
        if base_cells[base_index].cell_type != head_cells[head_index].cell_type:
            continue
        matched[base_index] = (head_index, "position")
        used_head.add(head_index)

    rows: List[_AlignmentRow] = []
    for base_index, (head_index, method) in matched.items():
        rows.append(
            _AlignmentRow(
                base_index=base_index,
                head_index=head_index,
                matched_by=method,
            )
        )

    for base_index in range(len(base_cells)):
        if base_index not in matched:
            rows.append(
                _AlignmentRow(
                    base_index=base_index,
                    head_index=None,
                    matched_by="unmatched",
                )
            )
    for head_index in range(len(head_cells)):
        if head_index not in used_head:
            rows.append(
                _AlignmentRow(
                    base_index=None,
                    head_index=head_index,
                    matched_by="unmatched",
                )
            )

    rows.sort(key=_alignment_sort_key)
    return rows


def _match_by_cell_id(
    base_cells: Sequence[_Cell],
    head_cells: Sequence[_Cell],
) -> List[Tuple[int, int]]:
    base_ids: Dict[str, List[int]] = {}
    head_ids: Dict[str, List[int]] = {}

    for cell in base_cells:
        if _is_valid_cell_id(cell.cell_id):
            base_ids.setdefault(cell.cell_id or "", []).append(cell.index)
    for cell in head_cells:
        if _is_valid_cell_id(cell.cell_id):
            head_ids.setdefault(cell.cell_id or "", []).append(cell.index)

    pairs: List[Tuple[int, int]] = []
    for cell_id in sorted(set(base_ids).intersection(head_ids)):
        base_indexes = base_ids[cell_id]
        head_indexes = head_ids[cell_id]
        if len(base_indexes) == 1 and len(head_indexes) == 1:
            pairs.append((base_indexes[0], head_indexes[0]))
    return pairs


def _is_valid_cell_id(cell_id: Optional[str]) -> bool:
    if cell_id is None:
        return False
    return VALID_CELL_ID_RE.fullmatch(cell_id) is not None


def _match_by_sequence(
    base_cells: Sequence[_Cell],
    head_cells: Sequence[_Cell],
    unmatched_base: Sequence[int],
    unmatched_head: Sequence[int],
) -> List[Tuple[int, int]]:
    if not unmatched_base or not unmatched_head:
        return []

    base_tokens = [_sequence_token(base_cells[idx]) for idx in unmatched_base]
    head_tokens = [_sequence_token(head_cells[idx]) for idx in unmatched_head]
    matcher = SequenceMatcher(None, base_tokens, head_tokens, autojunk=False)

    pairs: List[Tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            continue
        for offset in range(i2 - i1):
            pairs.append((unmatched_base[i1 + offset], unmatched_head[j1 + offset]))
    return pairs


def _sequence_token(cell: _Cell) -> Tuple[str, str, str, str]:
    return (
        cell.cell_type,
        _normalize_whitespace(cell.source),
        _output_signature(cell.outputs),
        _material_metadata_signature(cell.material_metadata),
    )


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _material_metadata_signature(metadata: Dict[str, Any]) -> str:
    return json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _output_signature(outputs: Sequence[Dict[str, Any]]) -> str:
    normalized = [_stable_jsonable(output) for output in outputs]
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _alignment_sort_key(row: _AlignmentRow) -> Tuple[int, int]:
    if row.head_index is not None and row.base_index is not None:
        return (row.head_index, 0)
    if row.head_index is not None:
        return (row.head_index, 1)
    if row.base_index is not None:
        return (row.base_index, 2)
    return (0, 3)


def _build_pair_diffs(
    base_cells: Sequence[_Cell],
    head_cells: Sequence[_Cell],
    rows: Sequence[_AlignmentRow],
    *,
    limits: DiffLimits,
) -> List[_PairDiff]:
    pair_diffs: List[_PairDiff] = []
    for row in rows:
        base_cell = base_cells[row.base_index] if row.base_index is not None else None
        head_cell = head_cells[row.head_index] if row.head_index is not None else None
        cell_type: CellType
        if head_cell is not None:
            cell_type = head_cell.cell_type
        elif base_cell is not None:
            cell_type = base_cell.cell_type
        else:
            continue

        source_changed = _source_for_compare(base_cell) != _source_for_compare(head_cell)
        outputs_changed = _outputs_for_compare(base_cell) != _outputs_for_compare(head_cell)
        material_metadata_changed = _metadata_for_compare(base_cell) != _metadata_for_compare(head_cell)
        output_changes: List[OutputChange] = []

        if outputs_changed or base_cell is None or head_cell is None:
            output_changes = _build_output_changes(base_cell, head_cell, limits=limits)

        pair_diffs.append(
            _PairDiff(
                base_index=row.base_index,
                head_index=row.head_index,
                cell_type=cell_type,
                base_source=base_cell.source if base_cell is not None else None,
                head_source=head_cell.source if head_cell is not None else None,
                source_changed=source_changed,
                outputs_changed=outputs_changed,
                material_metadata_changed=material_metadata_changed,
                output_changes=output_changes,
            )
        )
    return pair_diffs


def _source_for_compare(cell: Optional[_Cell]) -> str:
    if cell is None:
        return ""
    return _normalize_whitespace(cell.source)


def _outputs_for_compare(cell: Optional[_Cell]) -> str:
    if cell is None:
        return ""
    return _output_signature(cell.outputs)


def _metadata_for_compare(cell: Optional[_Cell]) -> str:
    if cell is None:
        return ""
    return _material_metadata_signature(cell.material_metadata)


def _build_output_changes(
    base_cell: Optional[_Cell],
    head_cell: Optional[_Cell],
    *,
    limits: DiffLimits,
) -> List[OutputChange]:
    if head_cell is not None:
        outputs = head_cell.outputs
    elif base_cell is not None:
        outputs = base_cell.outputs
    else:
        return []

    if not outputs and (base_cell is not None and base_cell.outputs):
        return [
            OutputChange(
                output_type="stream",
                mime_group="text",
                summary="outputs removed",
                truncated=False,
            )
        ]

    changes: List[OutputChange] = []
    for output in outputs:
        output_type = _normalize_output_type(output.get("output_type"))
        mime_group = _infer_mime_group(output_type, output)
        raw_size = _output_text_size(output)
        truncated = raw_size > limits.max_output_text_for_ai_chars
        changes.append(
            OutputChange(
                output_type=output_type,
                mime_group=mime_group,
                summary=_output_summary(output_type, mime_group, raw_size),
                truncated=truncated,
            )
        )
    return changes


def _normalize_output_type(raw_output_type: Any) -> OutputType:
    if raw_output_type in {"stream", "error", "display_data", "execute_result"}:
        return raw_output_type
    return "display_data"


def _infer_mime_group(output_type: OutputType, output: Dict[str, Any]) -> MimeGroup:
    if output_type in {"stream", "error"}:
        return "text"

    data = output.get("data")
    if not isinstance(data, dict):
        return "unknown"

    mime_keys = set(str(key) for key in data.keys())
    if any(key.startswith("image/") for key in mime_keys):
        return "image"
    if "text/html" in mime_keys:
        return "html"
    if any(key.endswith("+json") or key == "application/json" for key in mime_keys):
        return "json"
    if "text/csv" in mime_keys or "application/vnd.dataresource+json" in mime_keys:
        return "table"
    if "text/plain" in mime_keys:
        return "text"
    return "unknown"


def _output_text_size(output: Dict[str, Any]) -> int:
    text_parts: List[str] = []
    for key in ("text", "evalue"):
        value = output.get(key)
        if isinstance(value, str):
            text_parts.append(value)
        elif isinstance(value, list):
            text_parts.extend(part for part in value if isinstance(part, str))

    traceback = output.get("traceback")
    if isinstance(traceback, list):
        text_parts.extend(line for line in traceback if isinstance(line, str))

    data = output.get("data")
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, str):
                text_parts.append(value)
            elif isinstance(value, list):
                text_parts.extend(part for part in value if isinstance(part, str))
    return sum(len(part) for part in text_parts)


def _output_summary(output_type: OutputType, mime_group: MimeGroup, size: int) -> str:
    if output_type == "error":
        return f"error output updated ({size} chars)"
    if output_type == "stream":
        return f"text stream output updated ({size} chars)"
    return f"{mime_group} output updated ({size} chars)"


def _detect_moved_pairs(pair_diffs: Sequence[_PairDiff]) -> set[Tuple[int, int]]:
    unchanged_pairs: List[Tuple[int, int]] = []
    for pair in pair_diffs:
        if pair.base_index is None or pair.head_index is None:
            continue
        if pair.source_changed or pair.outputs_changed or pair.material_metadata_changed:
            continue
        unchanged_pairs.append((pair.base_index, pair.head_index))

    if not unchanged_pairs:
        return set()

    unchanged_pairs.sort(key=lambda pair: pair[0])
    head_indexes = [pair[1] for pair in unchanged_pairs]
    lis_positions = _longest_increasing_subsequence_positions(head_indexes)

    moved: set[Tuple[int, int]] = set()
    for position, pair in enumerate(unchanged_pairs):
        if position in lis_positions:
            continue
        moved.add(pair)
    return moved


def _longest_increasing_subsequence_positions(values: Sequence[int]) -> set[int]:
    if not values:
        return set()

    predecessors = [-1] * len(values)
    tails: List[int] = []
    tail_positions: List[int] = []

    for i, value in enumerate(values):
        pos = _bisect_tails(tails, value)
        if pos == len(tails):
            tails.append(value)
            tail_positions.append(i)
        else:
            tails[pos] = value
            tail_positions[pos] = i
        if pos > 0:
            predecessors[i] = tail_positions[pos - 1]

    lis_positions: set[int] = set()
    current = tail_positions[-1]
    while current != -1:
        lis_positions.add(current)
        current = predecessors[current]
    return lis_positions


def _bisect_tails(tails: Sequence[int], value: int) -> int:
    lo = 0
    hi = len(tails)
    while lo < hi:
        mid = (lo + hi) // 2
        if tails[mid] < value:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _classify_cell_change(
    pair: _PairDiff,
    moved_pairs: set[Tuple[int, int]],
) -> Optional[CellChangeType]:
    if pair.base_index is None:
        return "added"
    if pair.head_index is None:
        return "deleted"

    if not pair.source_changed and not pair.outputs_changed and not pair.material_metadata_changed:
        if (pair.base_index, pair.head_index) in moved_pairs:
            return "moved"
        return None

    if pair.outputs_changed and not pair.source_changed and not pair.material_metadata_changed:
        return "output_changed"
    return "modified"


def _pick_cell_id(
    base_cells: Sequence[_Cell],
    head_cells: Sequence[_Cell],
    base_index: Optional[int],
    head_index: Optional[int],
) -> Optional[str]:
    if head_index is not None:
        head_id = head_cells[head_index].cell_id
        if head_id is not None:
            return head_id
    if base_index is not None:
        return base_cells[base_index].cell_id
    return None


def _display_index(pair: _PairDiff) -> Optional[int]:
    if pair.head_index is not None:
        return pair.head_index + 1
    if pair.base_index is not None:
        return pair.base_index + 1
    return None


def _build_review_context(cells: Sequence[_Cell], index: Optional[int]) -> List[ContextCell]:
    if index is None:
        return []
    context: List[ContextCell] = []
    before = index - 1
    after = index + 1

    if before >= 0 and before < len(cells):
        context.append(
            ContextCell(
                relative_position="before",
                cell_type=cells[before].cell_type,
                summary=_context_cell_summary(cells[before]),
            )
        )
    if after >= 0 and after < len(cells):
        context.append(
            ContextCell(
                relative_position="after",
                cell_type=cells[after].cell_type,
                summary=_context_cell_summary(cells[after]),
            )
        )
    return context


def _context_cell_summary(cell: _Cell) -> str:
    source_chars = len(cell.source)
    source_lines = 0 if not cell.source else cell.source.count("\n") + 1
    outputs = len(cell.outputs)
    return (
        f"{cell.cell_type} cell context: {source_lines} lines, {source_chars} chars, "
        f"{outputs} outputs"
    )


def _cell_summary(change_type: CellChangeType, pair: _PairDiff) -> str:
    if change_type == "added":
        return "cell added"
    if change_type == "deleted":
        return "cell deleted"
    if change_type == "moved":
        return "cell reordered without material content changes"
    if change_type == "output_changed":
        return "cell outputs changed"

    segments: List[str] = []
    if pair.source_changed:
        segments.append("source")
    if pair.outputs_changed:
        segments.append("outputs")
    if pair.material_metadata_changed:
        segments.append("material metadata")
    if not segments:
        return "cell modified"
    return f"cell modified ({', '.join(segments)})"


def _summarize_notebook_metadata_change(
    base_nb: _ParsedNotebook,
    head_nb: _ParsedNotebook,
) -> Optional[str]:
    if base_nb.material_metadata == head_nb.material_metadata:
        return None
    return "notebook material metadata changed (kernelspec/language_info)"


def _metadata_summary(material_metadata_changed: bool) -> Optional[str]:
    if not material_metadata_changed:
        return None
    return "material metadata changed"


def notebook_diff_to_dict(notebook_diff: NotebookDiff) -> Dict[str, Any]:
    """Serialize a NotebookDiff into JSON-safe primitives."""
    return {
        "notebooks": [
            {
                "path": notebook.path,
                "change_type": notebook.change_type,
                "cell_changes": [
                    {
                        "locator": {
                            "cell_id": change.locator.cell_id,
                            "base_index": change.locator.base_index,
                            "head_index": change.locator.head_index,
                            "display_index": change.locator.display_index,
                        },
                        "cell_type": change.cell_type,
                        "change_type": change.change_type,
                        "summary": change.summary,
                        "source_changed": change.source_changed,
                        "outputs_changed": change.outputs_changed,
                        "material_metadata_changed": change.material_metadata_changed,
                        "output_changes": [
                            {
                                "output_type": output.output_type,
                                "mime_group": output.mime_group,
                                "summary": output.summary,
                                "truncated": output.truncated,
                            }
                            for output in change.output_changes
                        ],
                        "review_context": [
                            {
                                "relative_position": context.relative_position,
                                "cell_type": context.cell_type,
                                "summary": context.summary,
                            }
                            for context in change.review_context
                        ],
                    }
                    for change in notebook.cell_changes
                ],
                "notices": list(notebook.notices),
            }
            for notebook in notebook_diff.notebooks
        ],
        "total_notebooks_changed": notebook_diff.total_notebooks_changed,
        "total_cells_changed": notebook_diff.total_cells_changed,
        "notices": list(notebook_diff.notices),
    }


def iter_changed_notebook_paths(notebook_inputs: Iterable[NotebookInput]) -> List[str]:
    """Return changed notebook paths in deterministic input order."""
    return [item.path for item in notebook_inputs]
