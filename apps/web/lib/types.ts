export type SnapshotBlockKind = "source" | "outputs" | "metadata";
export type ReviewThreadStatus = "open" | "resolved" | "outdated";
export type ReviewSnapshotStatus = "pending" | "ready" | "failed";
export type GitHubHostKind = "github_com" | "ghes";
export type InstallationAccountType = "user" | "organization";
export type ReviewAssetMimeType = "image/png" | "image/jpeg" | "image/gif";
export type OutputItemChangeType = "added" | "removed" | "modified";
export type GitHubMirrorState = "pending" | "mirrored" | "failed" | "skipped";

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
    items: RenderOutputItem[];
  };
  metadata: {
    changed: boolean;
    summary: string | null;
  };
  review_context: ReviewContextItem[];
  thread_anchors: Record<SnapshotBlockKind, ThreadAnchor>;
};

export type RenderOutputPlaceholderItem = {
  kind: "placeholder";
  output_type: string;
  mime_group: string;
  summary: string;
  truncated: boolean;
  change_type: OutputItemChangeType;
};

export type RenderOutputImageItem = {
  kind: "image";
  asset_id: string;
  mime_type: ReviewAssetMimeType;
  width: number | null;
  height: number | null;
  change_type: OutputItemChangeType;
};

export type RenderOutputItem =
  | RenderOutputPlaceholderItem
  | RenderOutputImageItem;

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
  installation: {
    id: string;
    account_login: string;
    account_type: InstallationAccountType;
  };
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
  github_reply_comment_id?: number | null;
  github_reply_comment_url?: string | null;
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
  github_root_comment_id?: number | null;
  github_root_comment_url?: string | null;
  github_mirror_state?: GitHubMirrorState | null;
  github_last_mirrored_at?: string | null;
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

export type AiGatewayConfig = {
  id?: string;
  installation_id: string;
  provider_kind: "none" | "litellm";
  display_name: string | null;
  github_host_kind: GitHubHostKind | null;
  github_api_base_url: string | null;
  github_web_base_url: string | null;
  base_url: string | null;
  model_name: string | null;
  api_key_header_name: string | null;
  has_api_key: boolean;
  static_header_names: string[];
  use_responses_api: boolean;
  litellm_virtual_key_id: string | null;
  active: boolean;
  updated_by_github_user_id: number | null;
  updated_at: string | null;
};

export type AiGatewaySettingsResponse = {
  config: AiGatewayConfig;
};

export type AiGatewaySettingsRequest = {
  provider_kind: "litellm";
  display_name: string;
  github_host_kind: GitHubHostKind;
  github_api_base_url: string;
  github_web_base_url: string;
  base_url: string;
  model_name: string;
  api_key?: string;
  api_key_header_name: string;
  static_headers?: Record<string, string>;
  use_responses_api: boolean;
  litellm_virtual_key_id?: string;
  active: boolean;
};

export type AiGatewayTestResponse = {
  ok: true;
  provider_kind: "litellm";
  model_name: string;
  tested_endpoint: string;
};

export type AiGatewayFormValues = {
  display_name: string;
  github_host_kind: GitHubHostKind;
  github_api_base_url: string;
  github_web_base_url: string;
  base_url: string;
  model_name: string;
  api_key: string;
  api_key_header_name: string;
  replace_static_headers: boolean;
  static_headers_text: string;
  use_responses_api: boolean;
  litellm_virtual_key_id: string;
  active: boolean;
};

export type AiGatewayActionState = {
  notice: FlashNotice | null;
  config: AiGatewayConfig;
  form: AiGatewayFormValues;
  tested_endpoint: string | null;
};
