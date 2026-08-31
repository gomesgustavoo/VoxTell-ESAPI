// Thin typed fetch wrapper for the /v1 API.
//
// Types mirror api/schemas.py. Fields marked "additive" arrived with the dashboard
// rewrite and are optional here on purpose: the console must not break if it is
// ever served against an older API image than it was built for, which on a
// local-image cluster with IfNotPresent is a realistic rollback state.

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

export type JobState =
  | "awaiting_upload"
  | "queued"
  | "running"
  | "done"
  | "failed"
  | "cancelled"
  | "expired";

export const JOB_STATES: JobState[] = [
  "queued",
  "running",
  "done",
  "failed",
  "cancelled",
  "awaiting_upload",
  "expired",
];

export interface Job {
  job_id: string;
  state: JobState;
  progress: number;
  message: string | null;
  error: string | null;
  prompts: string[];
  queue_position: number | null;
  estimated_wait_seconds?: number | null;
  poll_after: number;
  has_mask: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  // additive
  queued_at?: string | null;
  duration_seconds?: number | null;
  gpu_seconds?: number | null;
  voxels?: number | null;
  bytes_in?: number | null;
  attempts?: number;
  failure_class?: string | null;
  volume_id?: string | null;
  // additive, with model addressing: a job now names either free-text prompts or
  // catalog structure ids, and reports which models actually ran.
  structure_ids?: string[] | null;
  models?: string[] | null;
}

export interface JobPage {
  jobs: Job[];
  total: number;
  limit: number;
  offset: number;
}

export interface Me {
  id: string;
  email: string | null;
  username: string | null;
  monthly_quota: number | null;
  used_this_month: number;
  outstanding: number;
  max_outstanding: number;
  capabilities?: string[];
  volume_ttl_minutes?: number | null;
  // additive
  queued?: number;
  running?: number;
  remaining?: number | null;
}

export interface UsageDay {
  day: string;
  jobs: number;
  prompts: number;
  gpu_seconds: number;
  voxels: number;
}

export interface Usage {
  days: UsageDay[];
  window_days: number;
  since: string;
  total_jobs: number;
  total_prompts: number;
  total_gpu_seconds: number;
}

/** Mirrors api/schemas.py::VolumeResponse. A "held series" in the UI: a CT the
 *  plugin uploaded once that further jobs can reuse until its TTL runs out. */
export interface Volume {
  volume_id: string;
  state: "uploading" | "ready" | "failed";
  content_sha256: string;
  bytes: number;
  voxels: number;
  x_size: number;
  y_size: number;
  z_size: number;
  jobs_run: number;
  created_at: string;
  expires_at: string;
}

export interface SystemState {
  queue_depth: number;
  running: number;
  worker_online: boolean;
  estimated_wait_seconds: number;
  snapshot_age_seconds: number;
}

/** Mirrors api/schemas.py::CatalogModel. */
export interface CatalogModel {
  key: string;
  display_name: string;
  /** "prompt" takes free text; anything else takes structure ids. */
  kind: string;
  region: string;
  modality: string;
  count: number | null;
  task: string | null;
  weights_variant: string | null;
  /** Shown, not hidden: only one CADS weights variant permits commercial use. */
  weights_licence: string;
  code_licence: string;
}

export interface CatalogStructure {
  id: string;
  display_name: string;
  group: string;
  modality: string;
  source_model: string;
  aliases: string[];
}

export interface CatalogPreset {
  key: string;
  display_name: string;
  structure_ids: string[];
  models: string[];
}

export interface Catalog {
  version: number;
  /** Render groups in this order. The server decides, not the client. */
  group_order: string[];
  models: CatalogModel[];
  structures: CatalogStructure[];
  presets: CatalogPreset[];
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

/** For the routes that carry no credential: /health, /auth/config, /models. */
async function publicRequest<T>(path: string): Promise<T> {
  const res = await fetch(`${env.apiBase}${path}`);
  if (!res.ok) throw new ApiError(res.status, `${res.status} ${res.statusText}`);
  return (await res.json()) as T;
}

export const api = {
  me: (token: string) => request<Me>(token, "/me"),

  // Unauthenticated, like /auth/config: it holds no patient data and nothing
  // tenant-specific, and it is the same answer for every caller. Served from the
  // API rather than compiled into either client, so adding a model is a server
  // deployment — which for the Eclipse plugin is the difference between a config
  // change and a physicist re-approving a DLL on every workstation.
  catalog: () => publicRequest<Catalog>("/models"),
  usage: (token: string, days = 30) => request<Usage>(token, `/usage?days=${days}`),
  system: (token: string) => request<SystemState>(token, "/system"),

  listKeys: (token: string) => request<ApiKey[]>(token, "/keys"),
  createKey: (token: string, name: string, expiresInDays: number | null) =>
    request<CreatedApiKey>(token, "/keys", {
      method: "POST",
      body: JSON.stringify({ name, expires_in_days: expiresInDays }),
    }),
  revokeKey: (token: string, id: string) =>
    request<void>(token, `/keys/${id}`, { method: "DELETE" }),

  listJobs: (
    token: string,
    opts: { limit?: number; offset?: number; state?: JobState | null } = {},
  ) => {
    const q = new URLSearchParams();
    q.set("limit", String(opts.limit ?? 25));
    if (opts.offset) q.set("offset", String(opts.offset));
    if (opts.state) q.set("state", opts.state);
    return request<JobPage>(token, `/jobs?${q}`);
  },
  cancelJob: (token: string, id: string) =>
    request<Job>(token, `/jobs/${id}/cancel`, { method: "POST" }),

  // Gated on Me.capabilities including "volumes" — the server derives that from
  // VOXTELL_VOLUMES_ENABLED, so a console built against a flag-on API must not
  // assume the route exists. Call it only when the capability is advertised.
  listVolumes: (token: string, limit = 20) =>
    request<{ volumes: Volume[] }>(token, `/volumes?limit=${limit}`),
  deleteVolume: (token: string, id: string) =>
    request<void>(token, `/volumes/${id}`, { method: "DELETE" }),

  job: (token: string, id: string) => request<Job>(token, `/jobs/${id}`),

  // ?redirect=false returns {url} instead of a 307.
  //
  // This is not a style preference. Following the 307 from a fetch that carries an
  // Authorization header turns the hop into a CORS request against
  // s3.dicomsegvr.com, whose -s3.allowedOrigins is dashboard.dicomsegvr.com ONLY —
  // so it fails preflight from this hostname. Asking for the URL and navigating to
  // it avoids CORS entirely.
  resultUrl: (token: string, id: string) =>
    request<{ url: string; filename: string }>(token, `/jobs/${id}/result?redirect=false`),
};
