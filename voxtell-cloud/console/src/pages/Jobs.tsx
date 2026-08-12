import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Job } from "../lib/api";
import { Alert, Button, Card, Empty, Progress, StateBadge } from "../components/ui";

const ACTIVE = new Set(["awaiting_upload", "queued", "running"]);

const fmt = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "short", timeStyle: "medium" }) : "—";

function duration(job: Job): string {
  if (!job.started_at) return "—";
  const end = job.finished_at ? new Date(job.finished_at) : new Date();
  const secs = Math.max(0, (end.getTime() - new Date(job.started_at).getTime()) / 1000);
  return secs < 90 ? `${secs.toFixed(0)}s` : `${(secs / 60).toFixed(1)}m`;
}

export function Jobs({ token }: { token: string }) {
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | null>(null);

  const load = useCallback(async () => {
    try {
      setJobs(await api.listJobs(token));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [token]);

  // Poll only while something is actually moving — an idle console should not
  // hit the API every few seconds forever.
  useEffect(() => {
    void load();
    const tick = async () => {
      await load();
      timer.current = window.setTimeout(() => void tick(), 5000);
    };
    timer.current = window.setTimeout(() => void tick(), 5000);
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [load]);

  const anyActive = jobs?.some((j) => ACTIVE.has(j.state)) ?? false;
  useEffect(() => {
    if (!anyActive && timer.current) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  }, [anyActive]);

  const cancel = async (job: Job) => {
    try {
      await api.cancelJob(token, job.job_id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const download = async (job: Job) => {
    // The API 307s to a presigned S3 URL; fetch follows it transparently.
    const res = await fetch(api.resultUrl(job.job_id), {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) {
      setError(`Download failed: ${res.status}`);
      return;
    }
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = `${job.job_id}-result.json.gz`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <Card
      title="Jobs"
      action={
        <Button variant="ghost" onClick={() => void load()}>
          Refresh
        </Button>
      }
    >
      {error && (
        <div className="mb-4">
          <Alert>{error}</Alert>
        </div>
      )}

      {jobs === null ? (
        <Empty>Loading…</Empty>
      ) : jobs.length === 0 ? (
        <Empty>
          No jobs yet. Segmentations submitted from Eclipse show up here.
        </Empty>
      ) : (
        <ul className="space-y-3">
          {jobs.map((job) => (
            <li
              key={job.job_id}
              className="rounded-lg border border-border bg-surface-2/40 p-4"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <StateBadge state={job.state} />
                    <span className="text-sm">{job.prompts.join(", ")}</span>
                  </div>
                  <p className="mono mt-1 text-xs text-muted">{job.job_id}</p>
                </div>
                <div className="flex items-center gap-2">
                  {job.state === "done" && (
                    <Button variant="ghost" onClick={() => void download(job)}>
                      Download
                    </Button>
                  )}
                  {ACTIVE.has(job.state) && (
                    <Button variant="danger" onClick={() => void cancel(job)}>
                      Cancel
                    </Button>
                  )}
                </div>
              </div>

              {ACTIVE.has(job.state) && (
                <div className="mt-3 space-y-1.5">
                  <Progress value={job.progress} />
                  <p className="text-xs text-muted">
                    {job.message ?? "Working…"}
                    {job.queue_position !== null &&
                      ` · ${job.queue_position} job(s) ahead`}
                  </p>
                </div>
              )}
              {job.error && (
                <p className="mt-2 text-xs text-danger">{job.error}</p>
              )}

              <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted">
                <div className="flex gap-1.5">
                  <dt>Created</dt>
                  <dd className="text-ink/80">{fmt(job.created_at)}</dd>
                </div>
                <div className="flex gap-1.5">
                  <dt>Duration</dt>
                  <dd className="text-ink/80">{duration(job)}</dd>
                </div>
              </dl>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
