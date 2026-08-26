// A job, as a ledger row.
//
// THE SHARED DEVICE. The landing page's hero writes a structure list — a short
// contour fragment in the structure's colour, the name in mono, the number
// right-aligned with tabular figures — because that is what Eclipse's structure list
// looks like and it is literally what VoxTell produces. This is the same row, with a
// job's prompts in place of a single structure. One device, two surfaces, so the
// marketing page and the signed-in app read as one product.

import { Link } from "react-router-dom";

import type { Job } from "../lib/api";
import { structureColour } from "../lib/structures";
import { bytes, duration, gpuTime, relative, stamp, voxels } from "../lib/format";
import { Button, Cell, Progress, PromptChip, Row, StateBadge } from "./ui";

/** The contour fragment, coloured from the job's first recognisable prompt. */
function LedgerMark({ prompts }: { prompts: string[] }) {
  const colour = prompts.map(structureColour).find(Boolean) ?? "var(--color-faint)" /* a UI colour, still a real @theme token */;
  return (
    <svg viewBox="0 0 26 12" className="h-3 w-[26px] flex-none overflow-visible" aria-hidden>
      <path
        d="M1 8C5 2 9 1 13 4S21 11 25 5"
        fill="none"
        stroke={colour}
        strokeWidth="1.7"
        strokeLinecap="round"
      />
    </svg>
  );
}

/** One line, for the Overview's "last five". A link, now that a job has an address. */
export function JobLine({ job }: { job: Job }) {
  return (
    <Link
      to={`/jobs/${job.job_id}`}
      className="flex items-center gap-3 border-b border-border-soft px-1 py-2.5 transition-colors last:border-0 hover:bg-surface-2"
    >
      <LedgerMark prompts={job.prompts} />
      <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink">
        {job.prompts.join(", ") || "—"}
      </span>
      <span className="flex-none font-mono text-xs text-muted tabular-nums">
        {job.prompts.length} {job.prompts.length === 1 ? "structure" : "structures"}
      </span>
      <StateBadge state={job.state} />
    </Link>
  );
}

/**
 * The compact row, for the Jobs page's table mode.
 *
 * 25 stacked cards is a very long scroll with no way to see a week of work at once,
 * and a queue is exactly the sort of thing people scan rather than read. This is the
 * same information at one line per job, using the shared Table primitives so the
 * columns line up with the Keys page.
 */
export function JobTableRow({
  job,
  onCancel,
  onDownload,
  busy,
}: {
  job: Job;
  onCancel: (id: string) => void;
  onDownload: (id: string) => void;
  busy: boolean;
}) {
  const active = job.state === "queued" || job.state === "running";
  return (
    <Row>
      <Cell>
        <StateBadge state={job.state} />
      </Cell>
      {/* `w-full max-w-0` is the table-truncation idiom: a <td> has no intrinsic
          width to truncate against, so `truncate` alone does nothing and a long
          prompt list simply widened the table until the action column scrolled off
          the right edge — you had to scroll sideways to reach Cancel. Zero max-width
          plus w-full makes this the flexible column and every other one intrinsic. */}
      <Cell className="w-full max-w-0">
        <Link
          to={`/jobs/${job.job_id}`}
          className="flex min-w-0 items-center gap-2 hover:text-accent"
        >
          <LedgerMark prompts={job.prompts} />
          <span className="truncate font-mono text-xs">{job.prompts.join(", ") || "—"}</span>
        </Link>
      </Cell>
      <Cell mono className="whitespace-nowrap">
        {duration(job.duration_seconds)}
      </Cell>
      <Cell mono className="whitespace-nowrap">
        {gpuTime(job.gpu_seconds)}
      </Cell>
      <Cell mono className="whitespace-nowrap">
        <span title={job.created_at}>{relative(job.created_at)}</span>
      </Cell>
      <Cell className="whitespace-nowrap">
        <div className="flex justify-end gap-1.5">
          {job.state === "done" && (
            <Button size="sm" disabled={busy} onClick={() => onDownload(job.job_id)}>
              Download
            </Button>
          )}
          {active && (
            <Button size="sm" variant="danger" disabled={busy} onClick={() => onCancel(job.job_id)}>
              Cancel
            </Button>
          )}
        </div>
      </Cell>
    </Row>
  );
}

/** The full row, for the Jobs page. */
export function JobCard({
  job,
  onCancel,
  onDownload,
  busy,
}: {
  job: Job;
  onCancel: (id: string) => void;
  onDownload: (id: string) => void;
  busy: boolean;
}) {
  const active = job.state === "queued" || job.state === "running";
  // Worth surfacing only when it says something: one attempt is the normal case.
  const requeued = (job.attempts ?? 0) > 1;

  return (
    <li className="rounded-card border border-border bg-surface">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border px-4 py-3">
        <LedgerMark prompts={job.prompts} />
        <StateBadge state={job.state} />
        <span className="ml-auto font-mono text-[11px] text-faint tabular-nums" title={job.created_at}>
          {stamp(job.created_at)} · {relative(job.created_at)}
        </span>
      </div>

      <div className="px-4 py-3">
        <div className="flex flex-wrap gap-1.5">
          {job.prompts.length ? (
            job.prompts.map((p, i) => <PromptChip key={`${p}-${i}`} prompt={p} />)
          ) : (
            <span className="text-xs text-muted">No prompts recorded</span>
          )}
        </div>

        {active && (
          <div className="mt-3">
            <Progress value={job.progress} label={job.message ?? "Working"} />
            {job.state === "queued" && (
              <p className="mt-1.5 font-mono text-[11px] text-muted tabular-nums">
                {job.queue_position != null
                  ? `${job.queue_position} job${job.queue_position === 1 ? "" : "s"} ahead`
                  : "waiting"}
                {job.estimated_wait_seconds != null &&
                  ` · about ${duration(job.estimated_wait_seconds)}`}
              </p>
            )}
          </div>
        )}

        {job.error && (
          <p className="mt-3 rounded-chip border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
            {job.error}
            {job.failure_class && (
              <span className="ml-1 font-mono text-[11px] opacity-70">({job.failure_class})</span>
            )}
          </p>
        )}

        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
          {[
            ["Duration", duration(job.duration_seconds)],
            ["GPU", gpuTime(job.gpu_seconds)],
            ["Voxels", voxels(job.voxels)],
            ["Uploaded", bytes(job.bytes_in)],
          ].map(([k, v]) => (
            <div key={k}>
              <dt className="font-mono text-[10px] tracking-label uppercase text-faint">{k}</dt>
              <dd className="font-mono text-xs text-ink-dim tabular-nums">{v}</dd>
            </div>
          ))}
          {requeued && (
            <div>
              <dt className="font-mono text-[10px] tracking-label uppercase text-faint">Attempts</dt>
              <dd className="font-mono text-xs text-ink-dim tabular-nums">{job.attempts}</dd>
            </div>
          )}
        </dl>
      </div>

      <div className="flex flex-wrap items-center gap-2 border-t border-border px-4 py-3">
        <Link
          to={`/jobs/${job.job_id}`}
          className="mr-auto font-mono text-[10px] text-faint hover:text-accent"
        >
          {job.job_id}
        </Link>
        {job.state === "done" && (
          <Button size="sm" disabled={busy} onClick={() => onDownload(job.job_id)}>
            Download
          </Button>
        )}
        {active && (
          <Button size="sm" variant="danger" disabled={busy} onClick={() => onCancel(job.job_id)}>
            Cancel
          </Button>
        )}
      </div>
    </li>
  );
}
