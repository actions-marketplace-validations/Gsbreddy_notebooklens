"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";

import { submitAiGatewaySettingsAction } from "@/lib/actions";
import type { AiGatewayActionState } from "@/lib/types";


type AiGatewaySettingsFormProps = {
  initialState: AiGatewayActionState;
  installationLabel: string;
  returnTo: string;
};


export function AiGatewaySettingsForm({
  initialState,
  installationLabel,
  returnTo,
}: AiGatewaySettingsFormProps) {
  const [state, formAction] = useActionState(
    submitAiGatewaySettingsAction,
    initialState,
  );
  const formKey = JSON.stringify({
    configUpdatedAt: state.config.updated_at,
    form: state.form,
    notice: state.notice,
    testedEndpoint: state.tested_endpoint,
  });

  return (
    <form action={formAction} className="settings-form" key={formKey}>
      <input name="returnTo" type="hidden" value={returnTo} />
      <input
        name="installationId"
        type="hidden"
        value={state.config.installation_id}
      />
      <input
        name="existingConfigJson"
        type="hidden"
        value={JSON.stringify(state.config)}
      />

      {state.notice ? (
        <div className={`flash-banner flash-${state.notice.tone}`}>
          {state.notice.message}
        </div>
      ) : null}

      <section className="settings-section">
        <div className="settings-section-head">
          <div>
            <p className="eyebrow">Gateway Identity</p>
            <h2>LiteLLM configuration</h2>
          </div>
          <span className="muted-copy">Installation: {installationLabel}</span>
        </div>
        <div className="settings-grid">
          <label>
            Display name
            <input
              defaultValue={state.form.display_name}
              name="displayName"
              required
              type="text"
            />
          </label>
          <label>
            Model name
            <input
              defaultValue={state.form.model_name}
              name="modelName"
              placeholder="gpt-4.1, claude-sonnet, ..."
              required
              type="text"
            />
          </label>
          <label>
            Base URL
            <input
              defaultValue={state.form.base_url}
              name="baseUrl"
              placeholder="https://litellm.internal.example/v1"
              required
              type="url"
            />
          </label>
          <label>
            API key header
            <input
              defaultValue={state.form.api_key_header_name}
              name="apiKeyHeaderName"
              required
              type="text"
            />
          </label>
          <label className="settings-grid-span">
            API key
            <input
              defaultValue={state.form.api_key}
              name="apiKey"
              placeholder={
                state.config.has_api_key
                  ? "Leave blank to keep the stored secret"
                  : "Bearer ... or provider token"
              }
              type="password"
            />
          </label>
          <label className="settings-grid-span">
            LiteLLM virtual key id
            <input
              defaultValue={state.form.litellm_virtual_key_id}
              name="litellmVirtualKeyId"
              placeholder="Optional virtual key identifier"
              type="text"
            />
          </label>
        </div>
      </section>

      <section className="settings-section">
        <div className="settings-section-head">
          <div>
            <p className="eyebrow">GitHub Host</p>
            <h2>Installation context</h2>
          </div>
          <span className="muted-copy">
            Match these values to the GitHub host that owns the installation.
          </span>
        </div>
        <div className="settings-grid">
          <label>
            GitHub host kind
            <select defaultValue={state.form.github_host_kind} name="githubHostKind">
              <option value="github_com">GitHub.com</option>
              <option value="ghes">GitHub Enterprise Server</option>
            </select>
          </label>
          <label>
            GitHub API base URL
            <input
              defaultValue={state.form.github_api_base_url}
              name="githubApiBaseUrl"
              required
              type="url"
            />
          </label>
          <label className="settings-grid-span">
            GitHub web base URL
            <input
              defaultValue={state.form.github_web_base_url}
              name="githubWebBaseUrl"
              required
              type="url"
            />
          </label>
        </div>
      </section>

      <section className="settings-section">
        <div className="settings-section-head">
          <div>
            <p className="eyebrow">Optional Headers</p>
            <h2>Static request headers</h2>
          </div>
          <span className="muted-copy">
            Add tenant or routing headers only when your gateway requires them.
          </span>
        </div>

        {state.config.static_header_names.length ? (
          <div className="settings-inline-note">
            Stored header names: {state.config.static_header_names.join(", ")}
          </div>
        ) : (
          <div className="settings-inline-note">
            No static headers are stored for this installation.
          </div>
        )}

        <label className="checkbox-row">
          <input
            defaultChecked={state.form.replace_static_headers}
            name="replaceStaticHeaders"
            type="checkbox"
          />
          Replace the stored static headers with the values below
        </label>

        <label>
          Header lines
          <textarea
            defaultValue={state.form.static_headers_text}
            name="staticHeadersText"
            placeholder={"X-Tenant-Token: secret\nX-Workspace: notebooklens"}
            rows={6}
          />
        </label>
      </section>

      <section className="settings-section">
        <div className="settings-section-head">
          <div>
            <p className="eyebrow">Gateway Mode</p>
            <h2>Activation and runtime</h2>
          </div>
          <span className="muted-copy">
            Testing does not activate the gateway; saving does.
          </span>
        </div>

        <div className="settings-toggle-stack">
          <label className="checkbox-row">
            <input
              defaultChecked={state.form.use_responses_api}
              name="useResponsesApi"
              type="checkbox"
            />
            Route requests through the OpenAI Responses-compatible endpoint
          </label>
          <label className="checkbox-row">
            <input
              defaultChecked={state.form.active}
              name="active"
              type="checkbox"
            />
            Activate this installation-scoped LiteLLM gateway for managed reviews
          </label>
        </div>

        {state.tested_endpoint ? (
          <div className="settings-inline-note">
            Last successful connection test used <code>{state.tested_endpoint}</code>.
          </div>
        ) : null}
      </section>

      <div className="settings-action-row">
        <SubmitButton label="Test connection" value="test" />
        <SubmitButton label="Save settings" value="save" primary />
      </div>
    </form>
  );
}


function SubmitButton({
  label,
  primary = false,
  value,
}: {
  label: string;
  primary?: boolean;
  value: "test" | "save";
}) {
  const { pending } = useFormStatus();

  return (
    <button
      className={primary ? "primary-button" : "secondary-button"}
      disabled={pending}
      name="intent"
      type="submit"
      value={value}
    >
      {pending ? "Working..." : label}
    </button>
  );
}
