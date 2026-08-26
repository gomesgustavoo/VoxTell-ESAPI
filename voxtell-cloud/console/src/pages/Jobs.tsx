// Jobs — filterable, paged, and polling only while something is actually moving.
//
// The bug this replaces: the old page started a 5 s poll, cleared it when nothing was
// active, and NEVER RESTARTED IT. A job submitted from Eclipse after the page settled
// never appeared until a manual refresh — on the one screen whose entire job is to
// show you that. react-query's `refetchInterval` accepts a function evaluated after
// every fetch, so "poll while anything is active" is the query's own state rather
// than a useEffect racing itself.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import { JOB_STATES, api, type JobPage, type JobState } from "../lib/api";
import { n } from "../lib/format";
import { useAuth } from "../auth/AuthProvider";
import { JobCard, JobTableRow } from "../components/JobRow";
import {
  Alert,
  Button,
  Empty,
  Modal,
  SectionHeader,
  Select,
  Skeleton,
  Table,
  ToastStack,
  useToasts,
} from "../components/ui";

const PAGE = 25;

// Density lives in the URL alongside the filter and the page, so a link to a job
// list carries how it was being read. Cards are the default because a first-time
// visitor with three jobs wants to see what a job contains, not scan a table.
type Density = "cards" | "table";
const TABLE_HEAD = ["State", "Prompts", "Duration", "GPU", "Submitted", ""];

export default function Jobs() {
  const { token } = useAuth();
  const qc = useQueryClient();
  const { toasts, push, dismiss } = useToasts();
  // Filter and page live in the URL, so a filtered view is linkable and the back
  // button works — neither was true when the whole console had no router.
  const [params, setParams] = useSearchParams();
  const state = (params.get("state") as JobState | null) ?? null;
  const offset = Math.max(0, Number(params.get("offset") ?? 0) || 0);
  const density: Density = params.get("view") === "table" ? "table" : "cards";
  const [confirmCancel, setConfirmCancel] = useState<string | null>(null);
  // WHICH row is busy, not whether ANY row is. `cancel.isPending` disabled the
  // buttons on all 25 rows while one mutation was in flight, which reads as the page
  // having locked up.
  const [busyId, setBusyId] = useState<string | null>(null);

  const jobs = useQuery<JobPage>({
    queryKey: ["jobs", { state, offset, limit: PAGE }],
    queryFn: () => api.listJobs(token!, { state, offset, limit: PAGE }),
    enabled: !!token,
    // Poll at the server's own suggested cadence, and only while a job can change.
    // `poll_after` is 5 s; falling back to it rather than hardcoding keeps the
    // client honouring a server-side change.
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return false;
      const active = data.jobs.some((j) => j.state === "queued" || j.state === "running");
      if (!active) return false;
      return Math.max(2, data.jobs[0]?.poll_after ?? 5) * 1000;
    },
  });

  const cancel = useMutation({
    mutationFn: (id: string) => {
      setBusyId(id);
      return api.cancelJob(token!, id);
    },
    onSuccess: () => {
      push("Cancellation requested.");
      void qc.invalidateQueries({ queryKey: ["jobs"] });
      void qc.invalidateQueries({ queryKey: ["me"] });
    },
    onError: (e: Error) => push(e.message, "danger"),
    onSettled: () => setBusyId(null),
  });

  const download = useMutation({
    mutationFn: (id: string) => {
      setBusyId(id);
      return api.resultUrl(token!, id);
    },
    onSuccess: ({ url }) => {
      // Plain navigation to the presigned URL. Fetching it instead would attach the
      // Authorization header and turn the S3 hop into a CORS request that
      // SeaweedFS's allowedOrigins does not permit from this hostname.
      window.location.assign(url);
    },
    onError: (e: Error) => push(e.message, "danger"),
    onSettled: () => setBusyId(null),
  });

  const total = jobs.data?.total ?? 0;
  const shown = jobs.data?.jobs.length ?? 0;
  const from = total === 0 ? 0 : offset + 1;

  function setFilter(next: string) {
    const p = new URLSearchParams(params);
    if (next) p.set("state", next);
    else p.delete("state");
    p.delete("offset");
    setParams(p, { replace: true });
  }

  function setDensity(next: Density) {
    const p = new URLSearchParams(params);
    if (next === "table") p.set("view", "table");
    else p.delete("view");
    setParams(p, { replace: true });
  }

  function page(delta: number) {
    const p = new URLSearchParams(params);
    const nextOffset = Math.max(0, offset + delta * PAGE);
    if (nextOffset) p.set("offset", String(nextOffset));
    else p.delete("offset");
    setParams(p);
  }

  return (
    <div>
      <SectionHeader
        label="Jobs"
        metric={total ? `${from}–${offset + shown} of ${n(total)}` : undefined}
      >
        Every segmentation submitted from Eclipse or the API. Prompts are coloured by
        the structure they name.
      </SectionHeader>

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Select
          aria-label="Filter by state"
          value={state ?? ""}
          onChange={(e) => setFilter(e.target.value)}
          className="max-w-48"
        >
          <option value="">All states</option>
          {JOB_STATES.map((s) => (
            <option key={s} value={s}>
              {s.replace("_", " ")}
            </option>
          ))}
        </Select>
        {/* A two-button group rather than a Select: two mutually exclusive options
            with no hidden state is exactly what a segmented control is for. */}
        <div className="flex overflow-hidden rounded-chip border border-border" role="group" aria-label="Row density">
          {(["cards", "table"] as const).map((d) => (
            <button
              key={d}
              type="button"
              onClick={() => setDensity(d)}
              aria-pressed={density === d}
              className={
                "px-2.5 py-1.5 font-mono text-[10px] tracking-label uppercase transition-colors " +
                (density === d
                  ? "bg-surface-2 text-accent"
                  : "text-faint hover:text-ink")
              }
            >
              {d}
            </button>
          ))}
        </div>
        {jobs.isFetching && (
          // aria-live, or the only signal that the list is updating itself is a
          // visual one.
          <span className="font-mono text-[10px] text-faint" role="status" aria-live="polite">
            refreshing…
          </span>
        )}
      </div>

      {jobs.isError && <Alert>Could not load jobs: {(jobs.error as Error).message}</Alert>}

      {jobs.isLoading ? (
        <div className="flex flex-col gap-3">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-40 w-full rounded-card" />
          ))}
        </div>
      ) : shown === 0 ? (
        <Empty>
          {state
            ? `No jobs in state “${state.replace("_", " ")}”.`
            : "No jobs yet. Segmentations submitted from Eclipse show up here."}
        </Empty>
      ) : density === "table" ? (
        <Table head={TABLE_HEAD}>
          {(jobs.data?.jobs ?? []).map((job) => (
            <JobTableRow
              key={job.job_id}
              job={job}
              busy={busyId === job.job_id}
              onCancel={(id) => setConfirmCancel(id)}
              onDownload={(id) => download.mutate(id)}
            />
          ))}
        </Table>
      ) : (
        <ul className="flex flex-col gap-3">
          {(jobs.data?.jobs ?? []).map((job) => (
            <JobCard
              key={job.job_id}
              job={job}
              busy={busyId === job.job_id}
              onCancel={(id) => setConfirmCancel(id)}
              onDownload={(id) => download.mutate(id)}
            />
          ))}
        </ul>
      )}

      {total > PAGE && (
        <div className="mt-5 flex items-center justify-between gap-3">
          <Button size="sm" disabled={offset === 0} onClick={() => page(-1)}>
            ← Newer
          </Button>
          <span className="font-mono text-xs text-faint tabular-nums">
            {from}–{offset + shown} of {n(total)}
          </span>
          <Button size="sm" disabled={offset + shown >= total} onClick={() => page(1)}>
            Older →
          </Button>
        </div>
      )}

      <Modal
        open={confirmCancel !== null}
        title="Cancel this job?"
        onClose={() => setConfirmCancel(null)}
        footer={
          <>
            <Button size="sm" variant="subtle" onClick={() => setConfirmCancel(null)}>
              Keep it
            </Button>
            <Button
              size="sm"
              variant="danger"
              onClick={() => {
                if (confirmCancel) cancel.mutate(confirmCancel);
                setConfirmCancel(null);
              }}
            >
              Cancel the job
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-dim">
          A queued job stops immediately. A running job unwinds at its next checkpoint.
        </p>
        <p className="mt-2 text-sm text-muted">
          It still counts against this month's quota — quota counts submissions, so
          cancelling does not refund it.
        </p>
      </Modal>

      <ToastStack toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
