// One job, at its own address.
//
// WHY THIS ROUTE EXISTS. Before it, a job id appeared in exactly one place — as
// unselectable-looking mono text at the bottom of a card in a 25-row list — and there
// was no way to link to a job at all. That matters more here than in most apps: the
// people who need to talk about a specific job are a planner at a workstation and
// whoever is looking at the queue, and "the liver one from this morning" is not an
// identifier. A job now has a URL you can paste into an email.
//
// It polls only while the job is actually active, and it honours the server's own
// `poll_after` rather than picking an interval — the same rule as the Jobs list, for
// the same reason: Traefik rate-limits /v1 and the server knows better than we do
// when the state can next have changed.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { api, ApiError } from "../lib/api";
import { bytes, duration, gpuTime, relative, stamp, voxels } from "../lib/format";
import { useAuth } from "../auth/AuthProvider";
import {
  Alert,
  Button,
  Card,
  Progress,
  PromptChip,
  SectionHeader,
  Skeleton,
  StateBadge,
  ToastStack,
  buttonClass,
  useToasts,
} from "../components/ui";

/** One label/value pair, mono and tabular, as the rest of the app reads numbers. */
function Fact({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div>
      <dt className="font-mono text-[10px] tracking-label uppercase text-faint">{label}</dt>
      <dd className="font-mono text-xs text-ink-dim tabular-nums" title={title}>
        {value}
      </dd>
    </div>
  );
}

export default function JobDetail() {
  const { id = "" } = useParams();
  const { token } = useAuth();
  const qc = useQueryClient();
  const { toasts, push, dismiss } = useToasts();

  const job = useQuery({
    queryKey: ["job", id],
    queryFn: () => api.job(token!, id),
    enabled: !!token && !!id,
    staleTime: 5_000,
    retry: (count, err) =>
      // A 404 here means "not your job, or never existed" — the server deliberately
      // returns 404 rather than 403 for another user's id. Retrying it is pointless
      // and makes the page sit on a spinner instead of saying so.
      !(err instanceof ApiError && err.status === 404) && count < 1,
    refetchInterval: (q) => {
      const d = q.state.data;
      if (!d) return false;
      if (d.state !== "queued" && d.state !== "running") return false;
      return Math.max(2, d.poll_after ?? 5) * 1000;
    },
  });

  const cancel = useMutation({
    mutationFn: () => api.cancelJob(token!, id),
    onSuccess: () => {
      push("Cancellation requested");
      void qc.invalidateQueries({ queryKey: ["job", id] });
      void qc.invalidateQueries({ queryKey: ["jobs"] });
      void qc.invalidateQueries({ queryKey: ["me"] });
    },
    onError: (e: Error) => push(e.message, "danger"),
  });

  const download = useMutation({
    mutationFn: () => api.resultUrl(token!, id),
    onSuccess: ({ url }) => window.location.assign(url),
    onError: (e: Error) => push(e.message, "danger"),
  });

  const d = job.data;
  const active = d?.state === "queued" || d?.state === "running";

  if (job.isError) {
    const err = job.error as Error;
    const gone = err instanceof ApiError && err.status === 404;
    return (
      <div className="flex flex-col gap-5">
        <SectionHeader label="Job" metric={id.slice(0, 8)}>
          {gone ? "No such job on this account." : "This job could not be loaded."}
        </SectionHeader>
        <Alert>
          {gone
            ? "Either the id is wrong, the job belonged to another account, or it was deleted. Finished jobs are purged 24 hours after they complete."
            : err.message}
        </Alert>
        <Link to="/jobs" className={`${buttonClass("ghost", "md")} self-start`}>
          All jobs
        </Link>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-5">
      <SectionHeader
        label="Job"
        metric={d ? `${d.prompts.length} prompt${d.prompts.length === 1 ? "" : "s"}` : "loading"}
      >
        {d
          ? `Submitted ${stamp(d.created_at)} · ${relative(d.created_at)}.`
          : "Reading the job record."}
      </SectionHeader>

      {job.isLoading || !d ? (
        <Skeleton className="h-64 w-full rounded-card" />
      ) : (
        <>
          <Card
            title="State"
            action={
              <div className="flex items-center gap-2">
                {d.state === "done" && (
                  <Button size="sm" disabled={download.isPending} onClick={() => download.mutate()}>
                    {download.isPending ? "Preparing…" : "Download"}
                  </Button>
                )}
                {active && (
                  <Button
                    size="sm"
                    variant="danger"
                    disabled={cancel.isPending}
                    onClick={() => cancel.mutate()}
                  >
                    {cancel.isPending ? "Cancelling…" : "Cancel"}
                  </Button>
                )}
              </div>
            }
          >
            <div className="flex flex-wrap items-center gap-3">
              <StateBadge state={d.state} />
              {job.isFetching && (
                <span className="font-mono text-[11px] text-faint" aria-live="polite">
                  refreshing…
                </span>
              )}
            </div>

            {active && (
              <div className="mt-4">
                <Progress value={d.progress} label={d.message ?? "Working"} />
                {d.state === "queued" && (
                  <p className="mt-1.5 font-mono text-[11px] text-muted tabular-nums">
                    {d.queue_position != null
                      ? `${d.queue_position} job${d.queue_position === 1 ? "" : "s"} ahead`
                      : "waiting"}
                    {d.estimated_wait_seconds != null &&
                      ` · about ${duration(d.estimated_wait_seconds)}`}
                  </p>
                )}
              </div>
            )}

            {d.error && (
              <p className="mt-4 rounded-chip border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
                {d.error}
                {d.failure_class && (
                  <span className="ml-1 font-mono text-[11px] opacity-70">
                    ({d.failure_class})
                  </span>
                )}
              </p>
            )}

            {d.state === "expired" && (
              <p className="mt-4 text-sm text-muted">
                The contours for this job have been deleted. Results are kept for 24 hours
                after a job finishes; the record stays so the job is still accounted for.
              </p>
            )}
          </Card>

          <Card title="Prompts" eyebrow={`${d.prompts.length} in this job`}>
            <div className="flex flex-wrap gap-1.5">
              {d.prompts.length ? (
                d.prompts.map((p, i) => <PromptChip key={`${p}-${i}`} prompt={p} />)
              ) : (
                <span className="text-xs text-muted">No prompts recorded.</span>
              )}
            </div>
          </Card>

          <Card title="Measurements">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4">
              <Fact label="Duration" value={duration(d.duration_seconds)} />
              <Fact label="GPU" value={gpuTime(d.gpu_seconds)} />
              <Fact label="Voxels" value={voxels(d.voxels)} />
              <Fact label="Uploaded" value={bytes(d.bytes_in)} />
              <Fact label="Queued" value={stamp(d.queued_at)} title={d.queued_at ?? undefined} />
              <Fact label="Started" value={stamp(d.started_at)} title={d.started_at ?? undefined} />
              <Fact
                label="Finished"
                value={stamp(d.finished_at)}
                title={d.finished_at ?? undefined}
              />
              {/* Only worth a cell when it says something: one attempt is normal. */}
              <Fact label="Attempts" value={String(d.attempts ?? 1)} />
            </dl>
          </Card>

          <Card title="Identifiers" padded={false}>
            <dl className="divide-y divide-border-soft">
              <div className="flex flex-wrap items-baseline gap-x-3 px-4 py-2.5">
                <dt className="font-mono text-[10px] tracking-label uppercase text-faint">Job</dt>
                <dd className="font-mono text-xs text-ink-dim select-all">{d.job_id}</dd>
              </div>
              <div className="flex flex-wrap items-baseline gap-x-3 px-4 py-2.5">
                <dt className="font-mono text-[10px] tracking-label uppercase text-faint">
                  Series
                </dt>
                <dd className="font-mono text-xs text-ink-dim select-all">
                  {d.volume_id ?? "inline upload — not held"}
                </dd>
              </div>
              <div className="flex flex-wrap items-baseline gap-x-3 px-4 py-2.5">
                <dt className="font-mono text-[10px] tracking-label uppercase text-faint">Mask</dt>
                <dd className="font-mono text-xs text-ink-dim">
                  {d.has_mask ? "requested" : "contours only"}
                </dd>
              </div>
            </dl>
          </Card>

          <Link to="/jobs" className={`${buttonClass("ghost", "md")} self-start`}>
            All jobs
          </Link>
        </>
      )}

      <ToastStack toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
