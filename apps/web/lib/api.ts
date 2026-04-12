import { cookies } from "next/headers";

import type { WorkspacePayload } from "@/lib/types";


export class ApiRequestError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiRequestError";
    this.status = status;
    this.detail = detail;
  }
}


export async function getReviewWorkspace(
  owner: string,
  repo: string,
  pullNumber: number,
): Promise<WorkspacePayload> {
  return apiRequest<WorkspacePayload>(
    `/api/reviews/${owner}/${repo}/pulls/${pullNumber}`,
  );
}


export async function getSnapshotWorkspace(
  owner: string,
  repo: string,
  pullNumber: number,
  snapshotIndex: number,
): Promise<WorkspacePayload> {
  return apiRequest<WorkspacePayload>(
    `/api/reviews/${owner}/${repo}/pulls/${pullNumber}/snapshots/${snapshotIndex}`,
  );
}


export async function postApi(
  path: string,
  body?: unknown,
): Promise<void> {
  await apiRequest(path, {
    method: "POST",
    body: body ? JSON.stringify(body) : undefined,
  });
}


export function buildLoginHref(nextPath: string): string {
  const url = new URL("/api/auth/github/login", getApiBaseUrl());
  url.searchParams.set("next_path", nextPath);
  return url.toString();
}


export async function postLogout(): Promise<void> {
  await apiRequest("/api/auth/logout", {
    method: "POST",
  });
}


async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const cookieStore = await cookies();
  const response = await fetch(new URL(path, getApiBaseUrl()), {
    ...init,
    cache: "no-store",
    headers: {
      Accept: "application/json",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...(cookieStore.size > 0 ? { Cookie: cookieStore.toString() } : {}),
      ...init.headers,
    },
  });

  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new ApiRequestError(response.status, detail);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}


async function readErrorDetail(response: Response): Promise<string> {
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    const payload = (await response.json()) as { detail?: string };
    if (typeof payload.detail === "string" && payload.detail.trim()) {
      return payload.detail;
    }
  }

  return response.statusText || "NotebookLens API request failed";
}


function getApiBaseUrl(): string {
  return (
    process.env.NOTEBOOKLENS_API_BASE_URL?.replace(/\/$/, "") ||
    "http://127.0.0.1:8000"
  );
}
