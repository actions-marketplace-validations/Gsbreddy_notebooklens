import type {
  FlashNotice,
  RenderRow,
  ReviewSnapshotRecord,
  ReviewThread,
  ThreadAnchor,
  WorkspaceReview,
} from "@/lib/types";


export function buildAnchorKey(anchor: ThreadAnchor): string {
  return JSON.stringify({
    notebook_path: anchor.notebook_path,
    block_kind: anchor.block_kind,
    source_fingerprint: anchor.source_fingerprint,
    cell_type: anchor.cell_type,
    cell_locator: {
      cell_id: anchor.cell_locator.cell_id ?? null,
      base_index: anchor.cell_locator.base_index ?? null,
      head_index: anchor.cell_locator.head_index ?? null,
      display_index: anchor.cell_locator.display_index ?? null,
    },
  });
}


export function groupThreadsByAnchor(threads: ReviewThread[]): Map<string, ReviewThread[]> {
  const buckets = new Map<string, ReviewThread[]>();

  for (const thread of threads) {
    const key = buildAnchorKey(thread.anchor);
    const existing = buckets.get(key);
    if (existing) {
      existing.push(thread);
      continue;
    }
    buckets.set(key, [thread]);
  }

  return buckets;
}


export function canStartThread(
  review: WorkspaceReview,
  snapshot: ReviewSnapshotRecord | null,
  row: RenderRow,
  blockKind: ThreadAnchor["block_kind"],
): boolean {
  if (snapshot === null) {
    return false;
  }
  if (snapshot.status !== "ready") {
    return false;
  }
  if (review.latest_snapshot_id !== snapshot.id) {
    return false;
  }
  return isBlockChanged(row, blockKind);
}


export function isBlockChanged(
  row: RenderRow,
  blockKind: ThreadAnchor["block_kind"],
): boolean {
  if (blockKind === "source") {
    return row.source.changed;
  }
  if (blockKind === "outputs") {
    return row.outputs.changed;
  }
  return row.metadata.changed;
}


export function buildSnapshotRoute(
  owner: string,
  repo: string,
  pullNumber: number,
  snapshotIndex: number | null,
): string {
  if (snapshotIndex === null) {
    return `/reviews/${owner}/${repo}/pulls/${pullNumber}`;
  }
  return `/reviews/${owner}/${repo}/pulls/${pullNumber}/snapshots/${snapshotIndex}`;
}


export function buildFlashRedirect(
  returnTo: string,
  notice: FlashNotice,
): string {
  const url = new URL(returnTo, "https://notebooklens.local");
  url.searchParams.set("flash", notice.tone);
  url.searchParams.set("message", notice.message);
  return `${url.pathname}?${url.searchParams.toString()}`;
}


export function readFlashNotice(
  searchParams: Record<string, string | string[] | undefined>,
): FlashNotice | null {
  const tone = firstValue(searchParams.flash);
  const message = firstValue(searchParams.message);
  if ((tone !== "success" && tone !== "error") || !message) {
    return null;
  }
  return {
    tone,
    message,
  };
}


export function formatCellLabel(row: RenderRow): string {
  const displayIndex = row.locator.display_index;
  const ordinal = displayIndex === null ? "Unknown" : `${displayIndex + 1}`;
  return `Cell ${ordinal}`;
}


export function summarizeFinding(
  finding: ReviewSnapshotRecord["flagged_findings"][number],
): string {
  return (
    finding.summary ??
    finding.message ??
    finding.code ??
    "Flagged notebook review finding"
  );
}


export function summarizeGuidance(
  guidance: ReviewSnapshotRecord["reviewer_guidance"][number],
): string {
  return (
    guidance.prompt ??
    guidance.label ??
    guidance.source ??
    "Reviewer guidance"
  );
}


function firstValue(value: string | string[] | undefined): string | null {
  if (Array.isArray(value)) {
    return value[0] ?? null;
  }
  return value ?? null;
}
