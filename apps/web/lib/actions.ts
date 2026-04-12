"use server";

import type { Route } from "next";
import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import { buildAiGatewayActionState, buildAiGatewaySettingsPayload } from "@/lib/ai-gateway";
import {
  ApiRequestError,
  buildLoginHref,
  postApi,
  postApiJson,
  postLogout,
  putApiJson,
} from "@/lib/api";
import { buildFlashRedirect } from "@/lib/review-workspace";
import type {
  AiGatewayActionState,
  AiGatewaySettingsResponse,
  AiGatewayTestResponse,
  AiGatewayFormValues,
} from "@/lib/types";


export async function createThreadAction(formData: FormData): Promise<never> {
  const returnTo = requiredField(formData, "returnTo");
  const reviewId = requiredField(formData, "reviewId");
  const snapshotId = requiredField(formData, "snapshotId");
  const bodyMarkdown = requiredField(formData, "bodyMarkdown");
  const anchorJson = requiredField(formData, "anchorJson");
  const anchor = JSON.parse(anchorJson) as unknown;

  try {
    await postApi(`/api/reviews/${reviewId}/threads`, {
      snapshot_id: snapshotId,
      anchor,
      body_markdown: bodyMarkdown,
    });
  } catch (error) {
    return handleMutationError(error, returnTo);
  }

  revalidatePath(returnTo);
  redirect(
    asRoute(buildFlashRedirect(returnTo, {
      tone: "success",
      message: "Thread created.",
    })),
  );
}


export async function replyToThreadAction(formData: FormData): Promise<never> {
  const returnTo = requiredField(formData, "returnTo");
  const threadId = requiredField(formData, "threadId");
  const bodyMarkdown = requiredField(formData, "bodyMarkdown");

  try {
    await postApi(`/api/threads/${threadId}/messages`, {
      body_markdown: bodyMarkdown,
    });
  } catch (error) {
    return handleMutationError(error, returnTo);
  }

  revalidatePath(returnTo);
  redirect(
    asRoute(buildFlashRedirect(returnTo, {
      tone: "success",
      message: "Reply added.",
    })),
  );
}


export async function resolveThreadAction(formData: FormData): Promise<never> {
  const returnTo = requiredField(formData, "returnTo");
  const threadId = requiredField(formData, "threadId");

  try {
    await postApi(`/api/threads/${threadId}/resolve`);
  } catch (error) {
    return handleMutationError(error, returnTo);
  }

  revalidatePath(returnTo);
  redirect(
    asRoute(buildFlashRedirect(returnTo, {
      tone: "success",
      message: "Thread resolved.",
    })),
  );
}


export async function reopenThreadAction(formData: FormData): Promise<never> {
  const returnTo = requiredField(formData, "returnTo");
  const threadId = requiredField(formData, "threadId");

  try {
    await postApi(`/api/threads/${threadId}/reopen`);
  } catch (error) {
    return handleMutationError(error, returnTo);
  }

  revalidatePath(returnTo);
  redirect(
    asRoute(buildFlashRedirect(returnTo, {
      tone: "success",
      message: "Thread reopened.",
    })),
  );
}


export async function logoutAction(formData: FormData): Promise<never> {
  const returnTo = requiredField(formData, "returnTo");

  try {
    await postLogout();
  } catch (error) {
    return handleMutationError(error, returnTo);
  }

  redirect(asRoute(buildLoginHref(returnTo)));
}


export async function submitAiGatewaySettingsAction(
  _previousState: AiGatewayActionState,
  formData: FormData,
): Promise<AiGatewayActionState> {
  const returnTo = requiredField(formData, "returnTo");
  const installationId = requiredField(formData, "installationId");
  const intent = requiredField(formData, "intent");
  const existingConfigJson = requiredField(formData, "existingConfigJson");
  const existingConfig = JSON.parse(existingConfigJson) as AiGatewayActionState["config"];
  const form = readAiGatewayFormValues(formData);

  let payload;
  try {
    payload = buildAiGatewaySettingsPayload(form);
  } catch (error) {
    return {
      config: existingConfig,
      form,
      tested_endpoint: null,
      notice: {
        tone: "error",
        message:
          error instanceof Error
            ? error.message
            : "NotebookLens could not parse the LiteLLM header values.",
      },
    };
  }

  try {
    if (intent === "test") {
      const result = await postApiJson<AiGatewayTestResponse>(
        `/api/settings/ai-gateway/test?installation_id=${encodeURIComponent(installationId)}`,
        payload,
      );
      return {
        config: existingConfig,
        form,
        tested_endpoint: result.tested_endpoint,
        notice: {
          tone: "success",
          message: `Connection test succeeded via ${result.tested_endpoint}.`,
        },
      };
    }

    const result = await putApiJson<AiGatewaySettingsResponse>(
      `/api/settings/ai-gateway?installation_id=${encodeURIComponent(installationId)}`,
      payload,
    );
    revalidatePath(asRoute(returnTo));
    return {
      ...buildAiGatewayActionState(result.config),
      notice: {
        tone: "success",
        message: "LiteLLM settings saved.",
      },
    };
  } catch (error) {
    if (error instanceof ApiRequestError && error.status === 401) {
      redirect(asRoute(buildLoginHref(returnTo)));
    }

    return {
      config: existingConfig,
      form,
      tested_endpoint: null,
      notice: {
        tone: "error",
        message:
          error instanceof ApiRequestError
            ? error.detail
            : "NotebookLens could not save those LiteLLM settings.",
      },
    };
  }
}


function requiredField(formData: FormData, key: string): string {
  const value = formData.get(key);
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new Error(`Missing required form field: ${key}`);
  }
  return value.trim();
}


function handleMutationError(error: unknown, returnTo: string): never {
  if (error instanceof ApiRequestError && error.status === 401) {
    redirect(asRoute(buildLoginHref(returnTo)));
  }

  const detail =
    error instanceof ApiRequestError
      ? error.detail
      : "NotebookLens could not complete that action.";
  redirect(
    asRoute(buildFlashRedirect(returnTo, {
      tone: "error",
      message: detail,
    })),
  );
}


function asRoute(value: string): Route {
  return value as Route;
}


function readAiGatewayFormValues(formData: FormData): AiGatewayFormValues {
  return {
    display_name: requiredField(formData, "displayName"),
    github_host_kind:
      requiredField(formData, "githubHostKind") === "ghes" ? "ghes" : "github_com",
    github_api_base_url: requiredField(formData, "githubApiBaseUrl"),
    github_web_base_url: requiredField(formData, "githubWebBaseUrl"),
    base_url: requiredField(formData, "baseUrl"),
    model_name: requiredField(formData, "modelName"),
    api_key: optionalField(formData, "apiKey"),
    api_key_header_name: requiredField(formData, "apiKeyHeaderName"),
    replace_static_headers: formData.get("replaceStaticHeaders") === "on",
    static_headers_text: optionalField(formData, "staticHeadersText"),
    use_responses_api: formData.get("useResponsesApi") === "on",
    litellm_virtual_key_id: optionalField(formData, "litellmVirtualKeyId"),
    active: formData.get("active") === "on",
  };
}


function optionalField(formData: FormData, key: string): string {
  const value = formData.get(key);
  return typeof value === "string" ? value : "";
}
