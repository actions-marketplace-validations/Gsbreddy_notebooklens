"""Shared review-core boundary for OSS action and managed review snapshot builders."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import struct
from typing import Any, Dict, List, Literal, Optional, Protocol, Sequence, Union

from .diff_engine import (
    CellChange,
    CellLocator,
    ContextCell,
    DiffLimits,
    NotebookDiff,
    NotebookFileDiff,
    NotebookInput,
    ReviewResult,
    build_notebook_diff,
)


SnapshotBlockKind = Literal["source", "outputs", "metadata"]
REVIEW_SNAPSHOT_SCHEMA_VERSION = 1
_SNAPSHOT_BLOCK_KINDS: Sequence[SnapshotBlockKind] = ("source", "outputs", "metadata")
ReviewAssetMimeType = Literal["image/png", "image/jpeg", "image/gif"]
OutputItemChangeType = Literal["added", "removed", "modified"]
_REVIEW_ASSET_ALLOWED_MIME_TYPES: Sequence[ReviewAssetMimeType] = (
    "image/png",
    "image/jpeg",
    "image/gif",
)
_REVIEW_ASSET_MAX_BYTES = 2_097_152


@dataclass(frozen=True)
class ReviewAssetDraft:
    """Extracted image asset awaiting snapshot-scoped persistence."""

    asset_key: str
    sha256: str
    mime_type: ReviewAssetMimeType
    byte_size: int
    width: int | None
    height: int | None
    content_bytes: bytes


@dataclass(frozen=True)
class _SnapshotCellContent:
    outputs: Sequence[Dict[str, Any]]


@dataclass(frozen=True)
class _SnapshotNotebookContent:
    base_cells: Sequence[_SnapshotCellContent]
    head_cells: Sequence[_SnapshotCellContent]


class ReviewCoreReviewer(Protocol):
    """Minimal reviewer contract shared by OSS and managed review flows."""

    def review(self, diff: NotebookDiff) -> ReviewResult:
        """Return structured review output for a notebook diff."""


@dataclass(frozen=True)
class ReviewCoreRequest:
    """Shared review-core input used by both the Action and managed services."""

    notebook_inputs: Sequence[NotebookInput]
    reviewer: ReviewCoreReviewer
    limits: DiffLimits = DiffLimits()
    snapshot_schema_version: int = REVIEW_SNAPSHOT_SCHEMA_VERSION


@dataclass(frozen=True)
class ReviewArtifacts:
    """Structured review outputs reusable by multiple runtime surfaces."""

    notebook_diff: NotebookDiff
    review_result: ReviewResult
    snapshot_payload: Dict[str, Any]
    review_assets: Sequence[ReviewAssetDraft]


def build_review_artifacts(request: ReviewCoreRequest) -> ReviewArtifacts:
    """Build reusable diff, review result, and normalized snapshot payload."""
    notebook_diff = build_notebook_diff(request.notebook_inputs, limits=request.limits)
    review_result = request.reviewer.review(notebook_diff)
    snapshot_payload, review_assets = _build_review_snapshot_payload(
        notebook_diff,
        schema_version=request.snapshot_schema_version,
        notebook_inputs=request.notebook_inputs,
    )
    return ReviewArtifacts(
        notebook_diff=notebook_diff,
        review_result=review_result,
        snapshot_payload=snapshot_payload,
        review_assets=review_assets,
    )


def build_review_snapshot_payload(
    notebook_diff: NotebookDiff,
    *,
    schema_version: int = REVIEW_SNAPSHOT_SCHEMA_VERSION,
    notebook_inputs: Sequence[NotebookInput] = (),
) -> Dict[str, Any]:
    """Build the versioned normalized review snapshot payload for hosted rendering."""
    snapshot_payload, _review_assets = _build_review_snapshot_payload(
        notebook_diff,
        schema_version=schema_version,
        notebook_inputs=notebook_inputs,
    )
    return snapshot_payload


def _build_review_snapshot_payload(
    notebook_diff: NotebookDiff,
    *,
    schema_version: int,
    notebook_inputs: Sequence[NotebookInput],
) -> tuple[Dict[str, Any], Sequence[ReviewAssetDraft]]:
    if schema_version != REVIEW_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported review snapshot schema version: {schema_version}"
        )

    snapshot_content_by_path = _snapshot_content_by_path(notebook_inputs)
    review_assets_by_key: Dict[str, ReviewAssetDraft] = {}
    snapshot_payload = {
        "schema_version": schema_version,
        "review": {
            "notices": list(notebook_diff.notices),
            "notebooks": [
                _notebook_snapshot(
                    notebook,
                    snapshot_content=snapshot_content_by_path.get(notebook.path),
                    review_assets_by_key=review_assets_by_key,
                )
                for notebook in notebook_diff.notebooks
            ],
        },
    }
    return snapshot_payload, tuple(review_assets_by_key.values())


def _notebook_snapshot(
    notebook: NotebookFileDiff,
    *,
    snapshot_content: _SnapshotNotebookContent | None,
    review_assets_by_key: Dict[str, ReviewAssetDraft],
) -> Dict[str, Any]:
    return {
        "path": notebook.path,
        "change_type": notebook.change_type,
        "notices": list(notebook.notices),
        "render_rows": [
            _render_row(
                notebook.path,
                change,
                snapshot_content=snapshot_content,
                review_assets_by_key=review_assets_by_key,
            )
            for change in notebook.cell_changes
        ],
    }


def _render_row(
    notebook_path: str,
    change: CellChange,
    *,
    snapshot_content: _SnapshotNotebookContent | None,
    review_assets_by_key: Dict[str, ReviewAssetDraft],
) -> Dict[str, Any]:
    return {
        "locator": _locator_dict(change.locator),
        "cell_type": change.cell_type,
        "change_type": change.change_type,
        "summary": change.summary,
        "source": {
            "base": change.base_source,
            "head": change.head_source,
            "changed": change.source_changed,
        },
        "outputs": {
            "changed": change.outputs_changed,
            "items": _render_output_items(
                change,
                snapshot_content=snapshot_content,
                review_assets_by_key=review_assets_by_key,
            ),
        },
        "metadata": {
            "changed": change.material_metadata_changed,
            "summary": change.metadata_summary,
        },
        "review_context": [_review_context_item(context) for context in change.review_context],
        "thread_anchors": {
            block_kind: _thread_anchor(
                notebook_path=notebook_path,
                change=change,
                block_kind=block_kind,
            )
            for block_kind in _SNAPSHOT_BLOCK_KINDS
        },
    }


def _thread_anchor(
    *,
    notebook_path: str,
    change: CellChange,
    block_kind: SnapshotBlockKind,
) -> Dict[str, Any]:
    return {
        "notebook_path": notebook_path,
        "cell_locator": _locator_dict(change.locator),
        "block_kind": block_kind,
        "source_fingerprint": _source_fingerprint(change),
        "cell_type": change.cell_type,
    }


def _locator_dict(locator: CellLocator) -> Dict[str, Optional[Union[int, str]]]:
    return {
        "cell_id": locator.cell_id,
        "base_index": locator.base_index,
        "head_index": locator.head_index,
        "display_index": locator.display_index,
    }


def _review_context_item(context: ContextCell) -> Dict[str, str]:
    return {
        "relative_position": context.relative_position,
        "cell_type": context.cell_type,
        "summary": context.summary,
    }


def _source_fingerprint(change: CellChange) -> str:
    source_text = change.head_source if change.head_source is not None else change.base_source
    normalized = _normalize_fingerprint_text(source_text or change.summary)
    payload = f"{change.cell_type}\0{normalized}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalize_fingerprint_text(value: str) -> str:
    return "\n".join(line.rstrip() for line in value.replace("\r", "").split("\n")).strip()


def _snapshot_content_by_path(
    notebook_inputs: Sequence[NotebookInput],
) -> Dict[str, _SnapshotNotebookContent]:
    return {
        notebook_input.path: _SnapshotNotebookContent(
            base_cells=_parse_snapshot_cells(notebook_input.base_content),
            head_cells=_parse_snapshot_cells(notebook_input.head_content),
        )
        for notebook_input in notebook_inputs
    }


def _parse_snapshot_cells(content: str | None) -> Sequence[_SnapshotCellContent]:
    if content is None:
        return ()
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return ()
    raw_cells = payload.get("cells")
    if not isinstance(raw_cells, list):
        return ()
    cells: List[_SnapshotCellContent] = []
    for raw_cell in raw_cells:
        if not isinstance(raw_cell, dict):
            continue
        cells.append(
            _SnapshotCellContent(
                outputs=_normalize_snapshot_outputs(raw_cell.get("outputs"))
            )
        )
    return tuple(cells)


def _normalize_snapshot_outputs(outputs: Any) -> Sequence[Dict[str, Any]]:
    if not isinstance(outputs, list):
        return ()
    normalized: List[Dict[str, Any]] = []
    for raw_output in outputs:
        if not isinstance(raw_output, dict):
            continue
        normalized_output: Dict[str, Any] = {}
        for key, value in raw_output.items():
            if key in {"execution_count", "metadata"}:
                continue
            normalized_output[str(key)] = _stable_jsonable(value)
        normalized.append(normalized_output)
    return tuple(normalized)


def _stable_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _stable_jsonable(val) for key, val in sorted(value.items())}
    if isinstance(value, list):
        return [_stable_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _render_output_items(
    change: CellChange,
    *,
    snapshot_content: _SnapshotNotebookContent | None,
    review_assets_by_key: Dict[str, ReviewAssetDraft],
) -> List[Dict[str, Any]]:
    if snapshot_content is None:
        return _fallback_output_items(change)

    base_outputs = _outputs_for_index(snapshot_content.base_cells, change.locator.base_index)
    head_outputs = _outputs_for_index(snapshot_content.head_cells, change.locator.head_index)
    selected_outputs, output_change_type = _select_output_render_source(
        base_outputs=base_outputs,
        head_outputs=head_outputs,
    )
    if not selected_outputs:
        return _fallback_output_items(change)

    items: List[Dict[str, Any]] = []
    for output in selected_outputs:
        rendered = _render_single_output_item(
            output,
            change_type=output_change_type,
            review_assets_by_key=review_assets_by_key,
        )
        items.append(rendered)
    return items or _fallback_output_items(change)


def _outputs_for_index(
    cells: Sequence[_SnapshotCellContent],
    index: int | None,
) -> Sequence[Dict[str, Any]]:
    if index is None or index < 0 or index >= len(cells):
        return ()
    return cells[index].outputs


def _select_output_render_source(
    *,
    base_outputs: Sequence[Dict[str, Any]],
    head_outputs: Sequence[Dict[str, Any]],
) -> tuple[Sequence[Dict[str, Any]], OutputItemChangeType]:
    if head_outputs and not base_outputs:
        return head_outputs, "added"
    if base_outputs and not head_outputs:
        return base_outputs, "removed"
    if head_outputs:
        return head_outputs, "modified"
    return (), "modified"


def _render_single_output_item(
    output: Dict[str, Any],
    *,
    change_type: OutputItemChangeType,
    review_assets_by_key: Dict[str, ReviewAssetDraft],
) -> Dict[str, Any]:
    output_type = _normalize_output_type(output.get("output_type"))
    mime_group = _infer_mime_group(output_type, output)
    image_payload = _build_image_output_item(
        output,
        change_type=change_type,
        review_assets_by_key=review_assets_by_key,
    )
    if image_payload is not None:
        return image_payload

    raw_size = _output_text_size(output)
    return {
        "kind": "placeholder",
        "output_type": output_type,
        "mime_group": mime_group,
        "summary": _output_summary(output_type, mime_group, raw_size),
        "truncated": False,
        "change_type": change_type,
    }


def _build_image_output_item(
    output: Dict[str, Any],
    *,
    change_type: OutputItemChangeType,
    review_assets_by_key: Dict[str, ReviewAssetDraft],
) -> Dict[str, Any] | None:
    image_candidate = _extract_image_candidate(output)
    if image_candidate is None:
        return None
    if image_candidate["status"] != "supported":
        return {
            "kind": "placeholder",
            "output_type": _normalize_output_type(output.get("output_type")),
            "mime_group": "image",
            "summary": image_candidate["summary"],
            "truncated": False,
            "change_type": change_type,
        }

    asset_draft = image_candidate["asset_draft"]
    review_assets_by_key.setdefault(asset_draft.asset_key, asset_draft)
    return {
        "kind": "image",
        "asset_key": asset_draft.asset_key,
        "mime_type": asset_draft.mime_type,
        "width": asset_draft.width,
        "height": asset_draft.height,
        "change_type": change_type,
    }


def _extract_image_candidate(output: Dict[str, Any]) -> Dict[str, Any] | None:
    data = output.get("data")
    if not isinstance(data, dict):
        return None

    image_keys = [str(key) for key in data.keys() if str(key).startswith("image/")]
    if not image_keys:
        return None

    supported_mime_type = next(
        (mime_type for mime_type in _REVIEW_ASSET_ALLOWED_MIME_TYPES if mime_type in data),
        None,
    )
    if supported_mime_type is None:
        unsupported_mime_type = sorted(image_keys)[0]
        return {
            "status": "placeholder",
            "summary": (
                f"{unsupported_mime_type} output kept as placeholder "
                "(unsupported image format)"
            ),
        }

    raw_payload = _normalize_mime_payload(data.get(supported_mime_type))
    if raw_payload is None or not raw_payload.strip():
        return {
            "status": "placeholder",
            "summary": (
                f"{supported_mime_type} output kept as placeholder "
                "(invalid image data)"
            ),
        }

    try:
        content_bytes = base64.b64decode(_normalize_base64(raw_payload), validate=False)
    except (ValueError, binascii.Error):
        return {
            "status": "placeholder",
            "summary": (
                f"{supported_mime_type} output kept as placeholder "
                "(invalid image data)"
            ),
        }

    byte_size = len(content_bytes)
    if byte_size > _REVIEW_ASSET_MAX_BYTES:
        return {
            "status": "placeholder",
            "summary": (
                f"{supported_mime_type} output kept as placeholder "
                f"({byte_size} bytes exceeds {_REVIEW_ASSET_MAX_BYTES} bytes)"
            ),
        }

    width, height = _image_dimensions(supported_mime_type, content_bytes)
    sha256 = hashlib.sha256(content_bytes).hexdigest()
    return {
        "status": "supported",
        "asset_draft": ReviewAssetDraft(
            asset_key=f"sha256:{sha256}",
            sha256=sha256,
            mime_type=supported_mime_type,
            byte_size=byte_size,
            width=width,
            height=height,
            content_bytes=content_bytes,
        ),
    }


def _normalize_mime_payload(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [part for part in value if isinstance(part, str)]
        return "".join(parts)
    return None


def _normalize_base64(value: str) -> str:
    return "".join(value.split())


def _image_dimensions(
    mime_type: ReviewAssetMimeType,
    content_bytes: bytes,
) -> tuple[int | None, int | None]:
    try:
        if mime_type == "image/png":
            return _png_dimensions(content_bytes)
        if mime_type == "image/gif":
            return _gif_dimensions(content_bytes)
        if mime_type == "image/jpeg":
            return _jpeg_dimensions(content_bytes)
    except ValueError:
        return None, None
    return None, None


def _png_dimensions(content_bytes: bytes) -> tuple[int, int]:
    if len(content_bytes) < 24 or content_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid png header")
    return struct.unpack(">II", content_bytes[16:24])


def _gif_dimensions(content_bytes: bytes) -> tuple[int, int]:
    if len(content_bytes) < 10 or content_bytes[:6] not in {b"GIF87a", b"GIF89a"}:
        raise ValueError("invalid gif header")
    return struct.unpack("<HH", content_bytes[6:10])


def _jpeg_dimensions(content_bytes: bytes) -> tuple[int, int]:
    if len(content_bytes) < 4 or content_bytes[:2] != b"\xff\xd8":
        raise ValueError("invalid jpeg header")
    offset = 2
    while offset + 9 < len(content_bytes):
        if content_bytes[offset] != 0xFF:
            offset += 1
            continue
        marker = content_bytes[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(content_bytes):
            break
        segment_length = struct.unpack(">H", content_bytes[offset : offset + 2])[0]
        if segment_length < 2 or offset + segment_length > len(content_bytes):
            break
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            if offset + 7 > len(content_bytes):
                break
            height, width = struct.unpack(">HH", content_bytes[offset + 3 : offset + 7])
            return width, height
        offset += segment_length
    raise ValueError("missing jpeg dimensions")


def _fallback_output_items(change: CellChange) -> List[Dict[str, Any]]:
    change_type: OutputItemChangeType = "modified"
    if change.change_type == "added":
        change_type = "added"
    elif change.change_type == "deleted":
        change_type = "removed"
    return [
        {
            "kind": "placeholder",
            "output_type": output.output_type,
            "mime_group": output.mime_group,
            "summary": output.summary,
            "truncated": output.truncated,
            "change_type": change_type,
        }
        for output in change.output_changes
    ]


def _normalize_output_type(raw_output_type: Any) -> str:
    if raw_output_type in {"stream", "error", "display_data", "execute_result"}:
        return str(raw_output_type)
    return "display_data"


def _infer_mime_group(output_type: str, output: Dict[str, Any]) -> str:
    if output_type in {"stream", "error"}:
        return "text"

    data = output.get("data")
    if not isinstance(data, dict):
        return "unknown"

    mime_keys = {str(key) for key in data.keys()}
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


def _output_summary(output_type: str, mime_group: str, size: int) -> str:
    if output_type == "error":
        return f"error output updated ({size} chars)"
    if output_type == "stream":
        return f"text stream output updated ({size} chars)"
    return f"{mime_group} output updated ({size} chars)"


__all__ = [
    "REVIEW_SNAPSHOT_SCHEMA_VERSION",
    "ReviewAssetDraft",
    "ReviewArtifacts",
    "ReviewCoreRequest",
    "ReviewCoreReviewer",
    "SnapshotBlockKind",
    "build_review_artifacts",
    "build_review_snapshot_payload",
]
