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
import { JobCard } from "../components/JobRow";
import {
  Alert,
  Button,
  Empty,
  Modal,
  SectionHeader,
  Select,
  Skeleton,
  ToastStack,
  useToasts,
} from "../components/ui";

const PAGE = 25;

export default function Jobs() {
  const { token } = useAuth();
  const qc = useQueryClient();
  const { toasts, push, dismiss } = useToasts();
  // Filter and page live in the URL, so a filtered view is linkable and the back
  // button works — neither was true when the whole console had no router.
  const [params, setParams] = useSearchParams();
  const state = (params.get("state") as JobState | null) ?? null;
  const offset = Math.max(0, Number(params.get("offset") ?? 0) || 0);
  const [confirmCancel, setConfirmCancel] = useState<string | null>(null);

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
    mutationFn: (id: string) => api.cancelJob(token!, id),
    onSuccess: () => {
      push("Cancellation requested.");
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["me"] });
    },
    onError: (e: Error) => push(e.message, "danger"),
  });

  const download = useMutation({
    mutationFn: (id: string) => api.resultUrl(token!, id),
    onSuccess: ({ url }) => {
      // Plain navigation to the presigned URL. Fetching it instead would attach the
      // Authorization header and turn the S3 hop into a CORS request that
      // SeaweedFS's allowedOrigins does not permit from this hostname.
      window.location.assign(url);
    },
    onError: (e: Error) => push(e.message, "danger"),
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
        {jobs.isFetching && <span className="font-mono text-[10px] text-faint">refreshing…</span>}
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
      ) : (
        <ul className="flex flex-col gap-3">
          {jobs.data!.jobs.map((job) => (
            <JobCard
              key={job.job_id}
              job={job}
              busy={cancel.isPending || download.isPending}
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
