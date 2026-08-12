// Thin typed fetch wrapper for the /v1 API.

import { env } from "./env";

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
}

export interface CreatedApiKey extends ApiKey {
  token: string;
}

export interface Job {
  job_id: string;
  state:
    | "awaiting_upload"
    | "queued"
    | "running"
    | "done"
    | "failed"
    | "cancelled"
    | "expired";
  progress: number;
  message: string | null;
  error: string | null;
  prompts: string[];
  queue_position: number | null;
  poll_after: number;
  has_mask: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface Me {
  id: string;
  email: string | null;
  username: string | null;
  monthly_quota: number | null;
  used_this_month: number;
  outstanding: number;
  max_outstanding: number;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(
  token: string,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const res = await fetch(`${env.apiBase}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  });

  if (!res.ok) {
    // FastAPI puts either a string or our {error, message} object in `detail`.
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      const detail = body?.detail;
      if (typeof detail === "string") message = detail;
      else if (detail?.message) message = detail.message;
      else if (detail?.error) message = detail.error;
    } catch {
      /* non-JSON error body — keep the status line */
    }
    throw new ApiError(res.status, message);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  me: (token: string) => request<Me>(token, "/me"),
  listKeys: (token: string) => request<ApiKey[]>(token, "/keys"),
  createKey: (token: string, name: string, expiresInDays: number | null) =>
    request<CreatedApiKey>(token, "/keys", {
      method: "POST",
      body: JSON.stringify({ name, expires_in_days: expiresInDays }),
    }),
  revokeKey: (token: string, id: string) =>
    request<void>(token, `/keys/${id}`, { method: "DELETE" }),
  listJobs: (token: string) =>
    request<{ jobs: Job[] }>(token, "/jobs?limit=50").then((r) => r.jobs),
  cancelJob: (token: string, id: string) =>
    request<Job>(token, `/jobs/${id}/cancel`, { method: "POST" }),
  // The API answers with a 307 to a short-lived presigned S3 URL; fetch follows
  // it, so this yields the gzipped result bytes directly.
  resultUrl: (id: string) => `${env.apiBase}/jobs/${id}/result`,
};
