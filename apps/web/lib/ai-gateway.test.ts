import { describe, expect, it } from "vitest";

import {
  buildAiGatewayActionState,
  buildAiGatewaySettingsPayload,
  parseStaticHeadersInput,
} from "@/lib/ai-gateway";
import type { AiGatewayConfig } from "@/lib/types";


function buildConfig(): AiGatewayConfig {
  return {
    installation_id: "installation-id",
    provider_kind: "litellm",
    display_name: "Internal LiteLLM",
    github_host_kind: "github_com",
    github_api_base_url: "https://api.github.com",
    github_web_base_url: "https://github.com",
    base_url: "https://litellm.internal.example/v1",
    model_name: "gpt-4.1",
    api_key_header_name: "Authorization",
    has_api_key: true,
    static_header_names: ["x-tenant-token"],
    use_responses_api: false,
    litellm_virtual_key_id: "vk-123",
    active: true,
    updated_by_github_user_id: 101,
    updated_at: "2026-04-12T12:00:00Z",
  };
}


describe("ai gateway helpers", () => {
  it("parses header input into a request payload", () => {
    const payload = buildAiGatewaySettingsPayload({
      display_name: " Internal LiteLLM ",
      github_host_kind: "ghes",
      github_api_base_url: " https://ghes-api.example.test ",
      github_web_base_url: " https://ghes.example.test ",
      base_url: " https://litellm.internal.example/v1 ",
      model_name: " claude-sonnet ",
      api_key: " secret-token ",
      api_key_header_name: " Authorization ",
      replace_static_headers: true,
      static_headers_text: "X-Tenant-Token: tenant\nX-Workspace: lens",
      use_responses_api: true,
      litellm_virtual_key_id: " vk-456 ",
      active: false,
    });

    expect(payload).toEqual({
      provider_kind: "litellm",
      display_name: "Internal LiteLLM",
      github_host_kind: "ghes",
      github_api_base_url: "https://ghes-api.example.test",
      github_web_base_url: "https://ghes.example.test",
      base_url: "https://litellm.internal.example/v1",
      model_name: "claude-sonnet",
      api_key: "secret-token",
      api_key_header_name: "Authorization",
      static_headers: {
        "X-Tenant-Token": "tenant",
        "X-Workspace": "lens",
      },
      use_responses_api: true,
      litellm_virtual_key_id: "vk-456",
      active: false,
    });
  });

  it("preserves existing static headers when replacement is disabled", () => {
    const payload = buildAiGatewaySettingsPayload({
      ...buildAiGatewayActionState(buildConfig()).form,
      replace_static_headers: false,
      static_headers_text: "",
    });

    expect(payload.static_headers).toBeUndefined();
  });

  it("rejects malformed static header lines", () => {
    expect(() => parseStaticHeadersInput("missing delimiter")).toThrow(
      "Static headers must use 'Header-Name: value' format.",
    );
  });
});
