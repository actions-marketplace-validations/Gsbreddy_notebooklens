import type { Route } from "next";
import Image from "next/image";
import Link from "next/link";

import {
  createThreadAction,
  logoutAction,
  reopenThreadAction,
  replyToThreadAction,
  resolveThreadAction,
} from "@/lib/actions";
import {
  buildApiHref,
  buildLoginHref,
} from "@/lib/api";
import {
  buildAnchorKey,
  buildAiGatewayRoute,
  buildSnapshotRoute,
  canStartThread,
  formatCellLabel,
  groupThreadsByAnchor,
  isBlockChanged,
  summarizeGitHubMirrorStatus,
  summarizeFinding,
  summarizeGuidance,
} from "@/lib/review-workspace";
import type {
  FlashNotice,
  RenderRow,
  ReviewSnapshotRecord,
  ReviewThread,
  SnapshotBlockKind,
  SnapshotNotebook,
  ThreadAnchor,
  WorkspacePayload,
} from "@/lib/types";


type ReviewWorkspaceProps = {
  workspace: WorkspacePayload;
  currentPath: string;
  flashNotice: FlashNotice | null;
};


export function ReviewWorkspace({
  workspace,
  currentPath,
  flashNotice,
}: ReviewWorkspaceProps) {
  const snapshot = workspace.snapshot;
  const threadsByAnchor = groupThreadsByAnchor(workspace.threads);

  return (
    <div className="workspace-shell">
      <header className="hero-card">
        <div className="hero-copy">
          <p className="eyebrow">NotebookLens Review Workspace</p>
          <h1>
            {workspace.review.owner}/{workspace.review.repo} PR #
            {workspace.review.pull_number}
          </h1>
          <p className="hero-summary">
            Review the latest normalized notebook snapshot, switch across prior
            revisions, and keep discussion anchored to specific changed notebook
            blocks.
          </p>
          <div className="hero-meta">
            <StatusPill label={`Review ${workspace.review.status}`} tone="default" />
            <StatusPill
              label={`${workspace.review.thread_counts.unresolved} open`}
              tone="accent"
            />
            <StatusPill
              label={`${workspace.review.thread_counts.resolved} resolved`}
              tone="success"
            />
            <StatusPill
              label={`${workspace.review.thread_counts.outdated} outdated`}
              tone="warning"
            />
          </div>
        </div>
        <div className="hero-actions">
          <Link
            className="secondary-button"
            href={
              buildAiGatewayRoute(
                workspace.review.owner,
                workspace.review.repo,
                workspace.review.pull_number,
              ) as Route
            }
          >
            LiteLLM settings
          </Link>
          <a className="secondary-button" href={buildLoginHref(currentPath)}>
            Refresh access
          </a>
          <form action={logoutAction}>
            <input name="returnTo" type="hidden" value={currentPath} />
            <button className="ghost-button" type="submit">
              Sign out
            </button>
          </form>
        </div>
      </header>

      {flashNotice ? (
        <div className={`flash-banner flash-${flashNotice.tone}`}>
          {flashNotice.message}
        </div>
      ) : null}

      <div className="workspace-grid">
        <main className="workspace-main">
          {snapshot ? (
            <SnapshotOverview review={workspace.review} snapshot={snapshot} />
          ) : (
            <EmptyState
              title="No review snapshot is available yet"
              description="The managed review exists, but there is not a selected snapshot to render."
            />
          )}

          {snapshot?.status === "failed" ? (
            <EmptyState
              title="Snapshot build failed"
              description={snapshot.failure_reason ?? "NotebookLens could not build this snapshot."}
            />
          ) : null}

          {snapshot?.status === "ready" &&
          snapshot.payload.review.notebooks.length > 0 ? (
            <section className="notebook-stack">
              {snapshot.payload.review.notebooks.map((notebook) => (
                <NotebookCard
                  currentPath={currentPath}
                  key={`${notebook.path}-${snapshot.id}`}
                  notebook={notebook}
                  reviewId={workspace.review.id}
                  review={workspace.review}
                  snapshot={snapshot}
                  threadsByAnchor={threadsByAnchor}
                />
              ))}
            </section>
          ) : null}

          {snapshot?.status === "ready" &&
          snapshot.payload.review.notebooks.length === 0 ? (
            <EmptyState
              title="No notebook diffs in this snapshot"
              description="NotebookLens did not persist any notebook-aware render rows for the selected revision."
            />
          ) : null}
        </main>

        <aside className="workspace-sidebar">
          <section className="side-card">
            <h2>Snapshot History</h2>
            <div className="history-list">
              {workspace.review.snapshot_history
                .slice()
                .reverse()
                .map((entry) => {
                  const href = entry.is_latest
                    ? buildSnapshotRoute(
                        workspace.review.owner,
                        workspace.review.repo,
                        workspace.review.pull_number,
                        null,
                      )
                    : buildSnapshotRoute(
                        workspace.review.owner,
                        workspace.review.repo,
                        workspace.review.pull_number,
                        entry.snapshot_index,
                      );

                  return (
                    <Link
                      className={`history-link ${
                        workspace.review.selected_snapshot_index === entry.snapshot_index
                          ? "history-link-active"
                          : ""
                      }`}
                      href={href as Route}
                      key={entry.id}
                    >
                      <span>
                        Snapshot {entry.snapshot_index}
                        {entry.is_latest ? " latest" : ""}
                      </span>
                      <span className="history-caption">{entry.head_sha.slice(0, 12)}</span>
                    </Link>
                  );
                })}
            </div>
          </section>

          <section className="side-card">
            <h2>Review Notes</h2>
            {snapshot?.payload.review.notices?.length ? (
              <ul className="chip-list">
                {snapshot.payload.review.notices.map((notice) => (
                  <li className="chip-item" key={notice}>
                    {notice}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted-copy">No global notices on this snapshot.</p>
            )}
          </section>

          <section className="side-card">
            <h2>Flagged Findings</h2>
            {snapshot?.flagged_findings?.length ? (
              <ul className="text-list">
                {snapshot.flagged_findings.map((finding, index) => (
                  <li key={`${finding.code ?? "finding"}-${index}`}>
                    {summarizeFinding(finding)}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted-copy">No deterministic findings were recorded.</p>
            )}
          </section>

          <section className="side-card">
            <h2>Reviewer Guidance</h2>
            {snapshot?.reviewer_guidance?.length ? (
              <ul className="text-list">
                {snapshot.reviewer_guidance.map((guidance, index) => (
                  <li key={`${guidance.label ?? "guidance"}-${index}`}>
                    {summarizeGuidance(guidance)}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted-copy">No reviewer guidance matched this snapshot.</p>
            )}
          </section>
        </aside>
      </div>
    </div>
  );
}


type SnapshotOverviewProps = {
  review: WorkspacePayload["review"];
  snapshot: ReviewSnapshotRecord;
};


function SnapshotOverview({ review, snapshot }: SnapshotOverviewProps) {
  return (
    <section className="summary-card">
      <div className="summary-head">
        <div>
          <p className="eyebrow">Selected Snapshot</p>
          <h2>Snapshot {snapshot.snapshot_index}</h2>
        </div>
        <StatusPill label={snapshot.status} tone={snapshot.status === "failed" ? "danger" : "default"} />
      </div>
      <div className="summary-grid">
        <div className="summary-metric">
          <span className="summary-label">Base branch</span>
          <strong>{review.base_branch}</strong>
        </div>
        <div className="summary-metric">
          <span className="summary-label">Base SHA</span>
          <strong>{snapshot.base_sha.slice(0, 12)}</strong>
        </div>
        <div className="summary-metric">
          <span className="summary-label">Head SHA</span>
          <strong>{snapshot.head_sha.slice(0, 12)}</strong>
        </div>
        <div className="summary-metric">
          <span className="summary-label">Notebooks</span>
          <strong>{snapshot.notebook_count}</strong>
        </div>
        <div className="summary-metric">
          <span className="summary-label">Changed cells</span>
          <strong>{snapshot.changed_cell_count}</strong>
        </div>
      </div>
      {snapshot.summary_text ? (
        <p className="summary-text">{snapshot.summary_text}</p>
      ) : null}
    </section>
  );
}


type NotebookCardProps = {
  review: WorkspacePayload["review"];
  reviewId: string;
  snapshot: ReviewSnapshotRecord;
  notebook: SnapshotNotebook;
  threadsByAnchor: Map<string, ReviewThread[]>;
  currentPath: string;
};


function NotebookCard({
  review,
  reviewId,
  snapshot,
  notebook,
  threadsByAnchor,
  currentPath,
}: NotebookCardProps) {
  return (
    <section className="notebook-card">
      <div className="notebook-head">
        <div>
          <p className="eyebrow">Notebook Diff</p>
          <h2>{notebook.path}</h2>
        </div>
        <StatusPill label={notebook.change_type} tone="default" />
      </div>

      {notebook.notices.length ? (
        <ul className="chip-list">
          {notebook.notices.map((notice) => (
            <li className="chip-item" key={notice}>
              {notice}
            </li>
          ))}
        </ul>
      ) : null}

      <div className="row-stack">
        {notebook.render_rows.map((row) => (
          <CellRowCard
            currentPath={currentPath}
            key={`${notebook.path}-${buildAnchorKey(row.thread_anchors.source)}`}
            notebookPath={notebook.path}
            review={review}
            reviewId={reviewId}
            row={row}
            snapshot={snapshot}
            threadsByAnchor={threadsByAnchor}
          />
        ))}
      </div>
    </section>
  );
}


type CellRowCardProps = {
  review: WorkspacePayload["review"];
  reviewId: string;
  snapshot: ReviewSnapshotRecord;
  row: RenderRow;
  notebookPath: string;
  threadsByAnchor: Map<string, ReviewThread[]>;
  currentPath: string;
};


function CellRowCard({
  review,
  reviewId,
  snapshot,
  row,
  notebookPath,
  threadsByAnchor,
  currentPath,
}: CellRowCardProps) {
  const blocks: SnapshotBlockKind[] = ["source", "outputs", "metadata"].filter(
    (blockKind) =>
      isBlockChanged(row, blockKind as SnapshotBlockKind) ||
      (threadsByAnchor.get(buildAnchorKey(row.thread_anchors[blockKind as SnapshotBlockKind]))?.length ?? 0) > 0,
  ) as SnapshotBlockKind[];

  return (
    <article className="cell-card">
      <div className="cell-card-head">
        <div>
          <p className="eyebrow">{formatCellLabel(row)}</p>
          <h3>
            {row.cell_type} cell · {row.change_type}
          </h3>
        </div>
        <p className="cell-summary">{row.summary}</p>
      </div>

      {row.review_context.length ? (
        <div className="context-strip">
          {row.review_context.map((context, index) => (
            <span className="context-pill" key={`${context.relative_position}-${index}`}>
              {context.relative_position}: {context.summary}
            </span>
          ))}
        </div>
      ) : null}

      <div className="block-stack">
        {blocks.map((blockKind) => {
          const anchor = row.thread_anchors[blockKind];
          const threads = threadsByAnchor.get(buildAnchorKey(anchor)) ?? [];
          const threadable = canStartThread(review, snapshot, row, blockKind);

          return (
            <section className="diff-block" key={blockKind}>
              <div className="diff-block-head">
                <div>
                  <p className="eyebrow">Inline Discussion</p>
                  <h4>{blockTitle(blockKind)}</h4>
                </div>
                <div className="diff-block-meta">
                  {isBlockChanged(row, blockKind) ? (
                    <StatusPill label="changed" tone="accent" />
                  ) : (
                    <StatusPill label="thread only" tone="default" />
                  )}
                  {threads.length ? (
                    <StatusPill label={`${threads.length} thread${threads.length === 1 ? "" : "s"}`} tone="default" />
                  ) : null}
                </div>
              </div>

              <BlockContent blockKind={blockKind} row={row} />

              <ThreadColumn
                anchor={anchor}
                currentPath={currentPath}
                reviewId={reviewId}
                snapshotId={snapshot.id}
                threadable={threadable}
                threads={threads}
              />
            </section>
          );
        })}
      </div>

      <p className="notebook-path-caption">{notebookPath}</p>
    </article>
  );
}


function BlockContent({
  blockKind,
  row,
}: {
  blockKind: SnapshotBlockKind;
  row: RenderRow;
}) {
  if (blockKind === "source") {
    return (
      <div className="code-grid">
        <CodePane label="Base source" value={row.source.base} />
        <CodePane label="Head source" value={row.source.head} />
      </div>
    );
  }

  if (blockKind === "outputs") {
    return (
      <div className="output-list">
        {row.outputs.items.length ? (
          row.outputs.items.map((item, index) => (
            item.kind === "image" ? (
              <ImageOutputCard item={item} key={`${item.asset_id}-${index}`} />
            ) : (
              <article className="output-card" key={`${item.output_type}-${index}`}>
                <div className="output-head">
                  <strong>{item.output_type}</strong>
                  <div className="output-meta">
                    <span>{item.mime_group}</span>
                    <StatusPill
                      label={item.change_type}
                      tone={outputChangeTone(item.change_type)}
                    />
                  </div>
                </div>
                <p>{item.summary}</p>
                {item.truncated ? (
                  <span className="muted-copy">Output summary truncated</span>
                ) : null}
              </article>
            )
          ))
        ) : (
          <p className="muted-copy">No output summaries were captured for this block.</p>
        )}
      </div>
    );
  }

  return (
    <div className="metadata-card">
      <p>{row.metadata.summary ?? "Notebook metadata changed."}</p>
    </div>
  );
}


function ImageOutputCard({
  item,
}: {
  item: Extract<RenderRow["outputs"]["items"][number], { kind: "image" }>;
}) {
  return (
    <article className="output-card image-output-card">
      <div className="output-head">
        <strong>Notebook image output</strong>
        <div className="output-meta">
          <span>{item.mime_type}</span>
          <StatusPill label={item.change_type} tone={outputChangeTone(item.change_type)} />
        </div>
      </div>
      <div className="output-image-frame">
        <Image
          alt={`Notebook output image (${item.mime_type})`}
          className="output-image"
          height={item.height ?? 675}
          loading="lazy"
          src={buildApiHref(`/api/review-assets/${item.asset_id}`)}
          unoptimized
          width={item.width ?? 1200}
        />
      </div>
      <p className="muted-copy">
        {item.width && item.height
          ? `${item.width} x ${item.height} px`
          : "Dimensions unavailable"}
      </p>
    </article>
  );
}


function CodePane({
  label,
  value,
}: {
  label: string;
  value: string | null;
}) {
  return (
    <div className="code-pane">
      <span className="code-pane-label">{label}</span>
      <pre>{value && value.length > 0 ? value : "No source on this side."}</pre>
    </div>
  );
}


type ThreadColumnProps = {
  reviewId: string;
  snapshotId: string;
  anchor: ThreadAnchor;
  threads: ReviewThread[];
  threadable: boolean;
  currentPath: string;
};


function ThreadColumn({
  reviewId,
  snapshotId,
  anchor,
  threads,
  threadable,
  currentPath,
}: ThreadColumnProps) {
  return (
    <div className="thread-column">
      <div className="thread-column-head">
        <h5>Inline threads</h5>
        <p className="muted-copy">
          Keep discussion attached to this diff block across snapshot history.
        </p>
      </div>

      {threadable ? (
        <details className="thread-composer">
          <summary>Start a thread</summary>
          <form action={createThreadAction} className="thread-form">
            <input name="returnTo" type="hidden" value={currentPath} />
            <input name="reviewId" type="hidden" value={reviewId} />
            <input name="snapshotId" type="hidden" value={snapshotId} />
            <input name="anchorJson" type="hidden" value={JSON.stringify(anchor)} />
            <label>
              Message
              <textarea
                name="bodyMarkdown"
                placeholder="Explain the regression, ask for notebook updates, or capture follow-up context."
                required
                rows={4}
              />
            </label>
            <button className="primary-button" type="submit">
              Create thread
            </button>
          </form>
        </details>
      ) : (
        <p className="muted-copy">
          New threads can only be created on changed blocks in the latest ready snapshot.
        </p>
      )}

      {threads.length ? (
        <div className="thread-stack">
          {threads.map((thread) => (
            <ThreadCard currentPath={currentPath} key={thread.id} thread={thread} />
          ))}
        </div>
      ) : (
        <p className="muted-copy">No threads are attached to this block yet.</p>
      )}
    </div>
  );
}


function ThreadCard({
  thread,
  currentPath,
}: {
  thread: ReviewThread;
  currentPath: string;
}) {
  const mirrorStatus = summarizeGitHubMirrorStatus(thread);

  return (
    <article className="thread-card">
      <div className="thread-head">
        <StatusPill label={thread.status} tone={threadTone(thread.status)} />
        {thread.carried_forward ? <StatusPill label="carried forward" tone="accent" /> : null}
      </div>

      <section className="mirror-card">
        <div className="mirror-head">
          <div>
            <p className="eyebrow">GitHub Mirror</p>
            <h6>{mirrorStatus.label}</h6>
          </div>
          <StatusPill label={mirrorStatus.label} tone={mirrorStatus.tone} />
        </div>
        <p className="muted-copy">{mirrorStatus.description}</p>
        <div className="mirror-links">
          {thread.github_root_comment_url && mirrorStatus.linkLabel ? (
            <a
              className="text-link"
              href={thread.github_root_comment_url}
              rel="noreferrer"
              target="_blank"
            >
              {mirrorStatus.linkLabel}
            </a>
          ) : null}
          {thread.github_last_mirrored_at ? (
            <span className="muted-copy">
              Last update {formatTimestamp(thread.github_last_mirrored_at)}
            </span>
          ) : null}
        </div>
      </section>

      <div className="message-stack">
        {thread.messages.map((message) => (
          <div className="message-card" key={message.id}>
            <div className="message-meta">
              <strong>{message.author_login}</strong>
              <div className="message-meta-links">
                <span>{formatTimestamp(message.created_at)}</span>
                {message.github_reply_comment_url ? (
                  <a
                    className="text-link"
                    href={message.github_reply_comment_url}
                    rel="noreferrer"
                    target="_blank"
                  >
                    Mirrored reply
                  </a>
                ) : null}
              </div>
            </div>
            <p className="message-body">{message.body_markdown}</p>
          </div>
        ))}
      </div>

      <div className="thread-actions">
        <details className="reply-details">
          <summary>Reply</summary>
          <form action={replyToThreadAction} className="thread-form">
            <input name="returnTo" type="hidden" value={currentPath} />
            <input name="threadId" type="hidden" value={thread.id} />
            <label>
              Message
              <textarea name="bodyMarkdown" required rows={3} />
            </label>
            <button className="primary-button" type="submit">
              Add reply
            </button>
          </form>
        </details>

        {thread.status === "resolved" ? (
          <form action={reopenThreadAction}>
            <input name="returnTo" type="hidden" value={currentPath} />
            <input name="threadId" type="hidden" value={thread.id} />
            <button className="secondary-button" type="submit">
              Reopen
            </button>
          </form>
        ) : (
          <form action={resolveThreadAction}>
            <input name="returnTo" type="hidden" value={currentPath} />
            <input name="threadId" type="hidden" value={thread.id} />
            <button className="secondary-button" type="submit">
              Resolve
            </button>
          </form>
        )}
      </div>
    </article>
  );
}


function StatusPill({
  label,
  tone,
}: {
  label: string;
  tone: "default" | "accent" | "success" | "warning" | "danger";
}) {
  return <span className={`status-pill tone-${tone}`}>{label}</span>;
}


function EmptyState({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <section className="summary-card">
      <h2>{title}</h2>
      <p className="muted-copy">{description}</p>
    </section>
  );
}


function blockTitle(blockKind: SnapshotBlockKind): string {
  if (blockKind === "source") {
    return "Source diff";
  }
  if (blockKind === "outputs") {
    return "Output summary diff";
  }
  return "Metadata diff";
}


function formatTimestamp(value: string): string {
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}


function threadTone(status: ReviewThread["status"]): "accent" | "success" | "warning" {
  if (status === "resolved") {
    return "success";
  }
  if (status === "outdated") {
    return "warning";
  }
  return "accent";
}


function outputChangeTone(
  changeType: "added" | "removed" | "modified",
): "accent" | "warning" | "default" {
  if (changeType === "added") {
    return "accent";
  }
  if (changeType === "removed") {
    return "warning";
  }
  return "default";
}
