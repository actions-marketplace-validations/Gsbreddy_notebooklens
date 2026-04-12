import Link from "next/link";

import { AiGatewaySettingsForm } from "@/components/ai-gateway-settings-form";
import { buildAiGatewayActionState } from "@/lib/ai-gateway";
import { buildSnapshotRoute } from "@/lib/review-workspace";
import type { AiGatewayConfig, WorkspaceReview } from "@/lib/types";


type AiGatewaySettingsProps = {
  review: WorkspaceReview;
  config: AiGatewayConfig;
  currentPath: string;
};


export function AiGatewaySettings({
  review,
  config,
  currentPath,
}: AiGatewaySettingsProps) {
  const reviewHref = buildSnapshotRoute(
    review.owner,
    review.repo,
    review.pull_number,
    null,
  );
  const installationLabel = `${review.installation.account_login} (${review.installation.account_type})`;

  return (
    <div className="workspace-shell">
      <header className="hero-card">
        <div className="hero-copy">
          <p className="eyebrow">Managed AI Settings</p>
          <h1>LiteLLM gateway for {review.owner}/{review.repo}</h1>
          <p className="hero-summary">
            Configure the installation-scoped gateway NotebookLens uses when
            managed AI review is enabled. The hosted workspace stays
            deterministic when the gateway is unavailable.
          </p>
          <div className="hero-meta">
            <span className={`status-pill ${config.active ? "tone-success" : "tone-default"}`}>
              {config.active ? "active gateway" : "deterministic only"}
            </span>
            <span className="status-pill tone-default">{installationLabel}</span>
            <span className="status-pill tone-accent">
              {config.provider_kind === "litellm" ? "LiteLLM configured" : "LiteLLM not configured"}
            </span>
          </div>
        </div>
        <div className="hero-actions">
          <Link className="secondary-button" href={reviewHref}>
            Back to review
          </Link>
        </div>
      </header>

      <div className="workspace-grid">
        <main className="workspace-main">
          <section className="summary-card">
            <div className="summary-head">
              <div>
                <p className="eyebrow">Installation Scope</p>
                <h2>{review.installation.account_login}</h2>
              </div>
              <span className="status-pill tone-default">
                {review.installation.account_type}
              </span>
            </div>
            <p className="summary-text">
              These settings apply to the GitHub App installation backing this
              managed workspace, not just this single pull request.
            </p>
          </section>

          <AiGatewaySettingsForm
            initialState={buildAiGatewayActionState(config)}
            installationLabel={installationLabel}
            returnTo={currentPath}
          />
        </main>

        <aside className="workspace-sidebar">
          <section className="side-card">
            <h2>Current Gateway</h2>
            <ul className="text-list">
              <li>
                Provider: {config.provider_kind === "litellm" ? "LiteLLM" : "None"}
              </li>
              <li>
                Model: {config.model_name ?? "Not configured"}
              </li>
              <li>
                API key: {config.has_api_key ? "Stored" : "Not stored"}
              </li>
              <li>
                Static headers:{" "}
                {config.static_header_names.length
                  ? config.static_header_names.join(", ")
                  : "None"}
              </li>
            </ul>
          </section>

          <section className="side-card">
            <h2>Notes</h2>
            <ul className="text-list">
              <li>Use connection testing before you activate a new model or base URL.</li>
              <li>Leaving the API key blank preserves the currently stored secret.</li>
              <li>
                Replacing static headers requires entering the full header set again because
                NotebookLens only reads back the stored header names.
              </li>
            </ul>
          </section>
        </aside>
      </div>
    </div>
  );
}
