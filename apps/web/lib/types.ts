export type SnapshotBlockKind = "source" | "outputs" | "metadata";
export type ReviewThreadStatus = "open" | "resolved" | "outdated";
export type ReviewSnapshotStatus = "pending" | "ready" | "failed";

export type CellLocator = {
  cell_id: string | null;
  base_index: number | null;
  head_index: number | null;
  display_index: number | null;
};

export type ThreadAnchor = {
  notebook_path: string;
  cell_locator: CellLocator;
  block_kind: SnapshotBlockKind;
  source_fingerprint: string;
  cell_type: "code" | "markdown" | "raw";
};

export type ReviewContextItem = {
  relative_position: string;
  cell_type: string;
  summary: string;
};

export type RenderRow = {
  locator: CellLocator;
  cell_type: "code" | "markdown" | "raw";
  change_type: string;
  summary: string;
  source: {
    base: string | null;
    head: string | null;
    changed: boolean;
  };
  outputs: {
    changed: boolean;
    items: Array<{
      output_type: string;
      mime_group: string;
      summary: string;
      truncated: boolean;
    }>;
  };
  metadata: {
    changed: boolean;
    summary: string | null;
  };
  review_context: ReviewContextItem[];
  thread_anchors: Record<SnapshotBlockKind, ThreadAnchor>;
};

export type SnapshotNotebook = {
  path: string;
  change_type: string;
  notices: string[];
  render_rows: RenderRow[];
};

export type ReviewSnapshotPayload = {
  schema_version: number;
  review: {
    notices: string[];
    notebooks: SnapshotNotebook[];
  };
};

export type SnapshotHistoryEntry = {
  id: string;
  snapshot_index: number;
  status: ReviewSnapshotStatus;
  base_sha: string;
  head_sha: string;
  created_at: string;
  is_latest: boolean;
};

export type WorkspaceReview = {
  id: string;
  owner: string;
  repo: string;
  pull_number: number;
  base_branch: string;
  status: string;
  latest_snapshot_id: string | null;
  latest_snapshot_index: number | null;
  selected_snapshot_index: number | null;
  thread_counts: {
    unresolved: number;
    resolved: number;
    outdated: number;
  };
  snapshot_history: SnapshotHistoryEntry[];
};

export type ReviewSnapshotRecord = {
  id: string;
  snapshot_index: number;
  status: ReviewSnapshotStatus;
  base_sha: string;
  head_sha: string;
  schema_version: number;
  summary_text: string | null;
  flagged_findings: Array<{
    code?: string;
    severity?: string;
    summary?: string;
    message?: string;
    [key: string]: unknown;
  }>;
  reviewer_guidance: Array<{
    label?: string;
    source?: string;
    prompt?: string;
    [key: string]: unknown;
  }>;
  payload: ReviewSnapshotPayload;
  notebook_count: number;
  changed_cell_count: number;
  failure_reason: string | null;
  created_at: string;
};

export type ThreadMessage = {
  id: string;
  author_github_user_id: number;
  author_login: string;
  body_markdown: string;
  created_at: string;
};

export type ReviewThread = {
  id: string;
  managed_review_id: string;
  origin_snapshot_id: string;
  current_snapshot_id: string;
  anchor: ThreadAnchor;
  status: ReviewThreadStatus;
  carried_forward: boolean;
  created_by_github_user_id: number;
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
  resolved_by_github_user_id: number | null;
  messages: ThreadMessage[];
};

export type WorkspacePayload = {
  review: WorkspaceReview;
  snapshot: ReviewSnapshotRecord | null;
  threads: ReviewThread[];
};

export type FlashNotice = {
  tone: "success" | "error";
  message: string;
};
