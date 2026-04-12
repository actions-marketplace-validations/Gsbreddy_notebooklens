import type {
  AiGatewayActionState,
  AiGatewayConfig,
  AiGatewayFormValues,
  AiGatewaySettingsRequest,
} from "@/lib/types";


export function buildAiGatewayFormValues(config: AiGatewayConfig): AiGatewayFormValues {
  const hasStoredHeaders = config.static_header_names.length > 0;

  return {
    display_name: config.display_name ?? "Internal LiteLLM",
    github_host_kind: config.github_host_kind ?? "github_com",
    github_api_base_url: config.github_api_base_url ?? "https://api.github.com",
    github_web_base_url: config.github_web_base_url ?? "https://github.com",
    base_url: config.base_url ?? "",
    model_name: config.model_name ?? "",
    api_key: "",
    api_key_header_name: config.api_key_header_name ?? "Authorization",
    replace_static_headers: !hasStoredHeaders,
    static_headers_text: "",
    use_responses_api: config.use_responses_api,
    litellm_virtual_key_id: config.litellm_virtual_key_id ?? "",
    active: config.active,
  };
}


export function buildAiGatewayActionState(
  config: AiGatewayConfig,
): AiGatewayActionState {
  return {
    notice: null,
    config,
    form: buildAiGatewayFormValues(config),
    tested_endpoint: null,
  };
}


export function buildAiGatewaySettingsPayload(
  form: AiGatewayFormValues,
): AiGatewaySettingsRequest {
  return {
    provider_kind: "litellm",
    display_name: form.display_name.trim(),
    github_host_kind: form.github_host_kind,
    github_api_base_url: form.github_api_base_url.trim(),
    github_web_base_url: form.github_web_base_url.trim(),
    base_url: form.base_url.trim(),
    model_name: form.model_name.trim(),
    api_key: form.api_key.trim() || undefined,
    api_key_header_name: form.api_key_header_name.trim(),
    static_headers: form.replace_static_headers
      ? parseStaticHeadersInput(form.static_headers_text)
      : undefined,
    use_responses_api: form.use_responses_api,
    litellm_virtual_key_id: form.litellm_virtual_key_id.trim() || undefined,
    active: form.active,
  };
}


export function parseStaticHeadersInput(value: string): Record<string, string> {
  const headers: Record<string, string> = {};
  const lines = value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);

  for (const line of lines) {
    const delimiter = line.indexOf(":");
    if (delimiter <= 0 || delimiter === line.length - 1) {
      throw new Error(
        `Static headers must use 'Header-Name: value' format. Invalid line: ${line}`,
      );
    }

    const key = line.slice(0, delimiter).trim();
    const headerValue = line.slice(delimiter + 1).trim();
    if (!key || !headerValue) {
      throw new Error(
        `Static headers must use 'Header-Name: value' format. Invalid line: ${line}`,
      );
    }
    if (Object.hasOwn(headers, key)) {
      throw new Error(`Duplicate static header: ${key}`);
    }
    headers[key] = headerValue;
  }

  return headers;
}
