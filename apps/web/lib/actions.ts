"use server";

import type { Route } from "next";
import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import { ApiRequestError, buildLoginHref, postApi, postLogout } from "@/lib/api";
import { buildFlashRedirect } from "@/lib/review-workspace";


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
