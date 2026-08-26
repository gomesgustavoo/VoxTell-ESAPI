// Overview — the month, the queue, and the last five jobs.
//
// This replaces one stale sentence. The whole usage display used to be
// "12 of 200 jobs used this month · 1/2 in flight", fetched once per session, never
// refreshed, with errors swallowed so the line simply vanished. There was no trend,
// no idea whether that 12 arrived today or over three weeks, and no way to see why a
// job was waiting.

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api } from "../lib/api";
import { duration, gpuTime, n, daysLeftInMonth } from "../lib/format";
import { useAuth } from "../auth/AuthProvider";
import { ColumnChart, DonutRing, QuotaMeter } from "../components/charts";
import { JobLine } from "../components/JobRow";
import { Alert, Card, Empty, SectionHeader, Skeleton, StatCard, buttonClass } from "../components/ui";

const WINDOW_DAYS = 28;

export default function Overview() {
  const { token } = useAuth();

  // staleTime rather than an interval: /me and /usage change when a job is
  // submitted, and this page is not where a user watches one run. The Traefik rate
  // limit (average 100/s, burst 200, keyed on Cf-Connecting-IP) applies to /v1, so
  // polling budget is spent on the Jobs page, where it buys something.
  const me = useQuery({
    queryKey: ["me"],
    queryFn: () => api.me(token!),
    enabled: !!token,
    staleTime: 30_000,
  });
  const usage = useQuery({
    queryKey: ["usage", WINDOW_DAYS],
    queryFn: () => api.usage(token!, WINDOW_DAYS),
    enabled: !!token,
    staleTime: 300_000,
  });
  const system = useQuery({
    queryKey: ["system"],
    queryFn: () => api.system(token!),
    enabled: !!token,
    // The one thing here worth refreshing on its own: it explains a wait.
    refetchInterval: 20_000,
    staleTime: 10_000,
  });
  const recent = useQuery({
    queryKey: ["jobs", { limit: 5 }],
    queryFn: () => api.listJobs(token!, { limit: 5 }),
    enabled: !!token,
    staleTime: 15_000,
  });

  const inFlight = (me.data?.queued ?? 0) + (me.data?.running ?? 0);
  const daysLeft = daysLeftInMonth();

  return (
    <div className="flex flex-col gap-8">
      <SectionHeader
        label="This month"
        metric={
          me.data
            ? `${n(me.data.used_this_month)} submitted · ${daysLeft} day${daysLeft === 1 ? "" : "s"} left`
            : undefined
        }
      >
        Quota counts submissions, not completions, and resets on the 1st (UTC).
        Cancelling a job does not refund it.
      </SectionHeader>

      {me.isError && <Alert>Could not load your account: {(me.error as Error).message}</Alert>}

      <div className="grid gap-4 lg:grid-cols-[1.4fr_1fr]">
        <Card title="Monthly quota">
          {me.isLoading ? (
            <div className="flex flex-col gap-3">
              <Skeleton className="h-7 w-40" />
              <Skeleton className="h-3 w-full" />
            </div>
          ) : me.data ? (
            <QuotaMeter
              used={me.data.used_this_month}
              limit={me.data.monthly_quota}
              inFlight={inFlight}
            />
          ) : (
            <p className="text-sm text-muted">Unavailable.</p>
          )}
        </Card>

        <Card title="Concurrency">
          {me.data ? (
            <DonutRing
              value={me.data.outstanding}
              max={me.data.max_outstanding}
              label={`${me.data.running ?? 0} on the GPU, ${me.data.queued ?? 0} waiting`}
              sub={`Your cap is ${me.data.max_outstanding} outstanding job${
                me.data.max_outstanding === 1 ? "" : "s"
              }.`}
            />
          ) : (
            <Skeleton className="h-20 w-full" />
          )}
        </Card>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          label={`Jobs / ${WINDOW_DAYS}d`}
          value={usage.data ? n(usage.data.total_jobs) : "—"}
          hint={usage.data ? `${n(usage.data.total_prompts)} structures requested` : undefined}
        />
        <StatCard
          label={`GPU / ${WINDOW_DAYS}d`}
          value={usage.data ? gpuTime(usage.data.total_gpu_seconds).split(" ")[0] : "—"}
          unit={usage.data ? gpuTime(usage.data.total_gpu_seconds).split(" ")[1] : undefined}
        />
        <StatCard
          label="Queue depth"
          value={system.data ? n(system.data.queue_depth) : "—"}
          tone={system.data && system.data.queue_depth > 0 ? "accent" : "ink"}
          hint={
            system.data
              ? system.data.queue_depth > 0
                ? `about ${duration(system.data.estimated_wait_seconds)} to start`
                : "no backlog"
              : undefined
          }
        />
        <StatCard
          label="Worker"
          value={system.data ? (system.data.worker_online ? "online" : "offline") : "—"}
          tone={system.data ? (system.data.worker_online ? "ok" : "danger") : "muted"}
          hint={
            system.data
              ? `${n(system.data.running)} running · snapshot ${Math.round(
                  system.data.snapshot_age_seconds,
                )}s old`
              : undefined
          }
        />
      </div>

      <Card title="Daily activity" eyebrow="Usage">
        {usage.isLoading ? (
          <Skeleton className="h-32 w-full" />
        ) : usage.isError ? (
          <Alert>Could not load usage: {(usage.error as Error).message}</Alert>
        ) : usage.data && usage.data.total_jobs === 0 ? (
          <Empty
            action={
              // A link styled as a button, not a button wrapping a link — see
              // buttonClass() in ui.tsx for why that distinction matters.
              <Link to="/keys" className={buttonClass("primary", "sm")}>
                Create a workstation key
              </Link>
            }
          >
            No jobs in the last {WINDOW_DAYS} days. Segmentations submitted from Eclipse
            appear here.
          </Empty>
        ) : (
          usage.data && <ColumnChart days={usage.data.days} />
        )}
      </Card>

      <Card
        title="Recent jobs"
        action={
          <Link
            to="/jobs"
            className="font-mono text-xs uppercase tracking-label text-muted hover:text-accent"
          >
            All jobs →
          </Link>
        }
      >
        {recent.isLoading ? (
          <div className="flex flex-col gap-2">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-8 w-full" />
            ))}
          </div>
        ) : recent.data?.jobs.length ? (
          <div>
            {recent.data.jobs.map((j) => (
              <JobLine key={j.job_id} job={j} />
            ))}
          </div>
        ) : (
          <Empty>Nothing yet. Submit a segmentation from the Eclipse plugin.</Empty>
        )}
      </Card>
    </div>
  );
}
