// Hand-rolled SVG charts.
//
// Three shapes are needed — a quota meter, a daily column series, a ring — and a
// charting library is 40-120 KB for that. The no-CDN rule (a hospital workstation
// may have no outbound internet, the same constraint that keeps oidc-client-ts
// bundled) means every one of those kilobytes lands in our own bundle, so these are
// written by hand against the design tokens.

import { useId, useState } from "react";

import type { UsageDay } from "../lib/api";
import { duration, monthProgress, n, shortDay } from "../lib/format";

/**
 * A DOM id that is safe inside `url(#…)`.
 *
 * React's `useId` returns something like `:r1:` — deliberately, to avoid colliding
 * with author ids. Colons are not valid in an unescaped CSS/SVG fragment identifier,
 * so `fill="url(#:r1:)"` silently resolves to nothing and the shape renders
 * transparent. Stripping to word characters is the standard fix and keeps the
 * uniqueness guarantee, which is the reason to use the hook at all.
 */
function useSvgId(prefix: string): string {
  return `${prefix}-${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
}

/* ------------------------------------------------------------------ meter */

/**
 * The monthly quota, as a burn-down with a PACE MARKER.
 *
 * The marker is the whole point and it is why this is not a plain progress bar.
 * "34 of 200 used" does not answer the question a planner actually has, which is
 * *am I ahead of my burn or behind it*. The tick sits at the fraction of the month
 * elapsed, so a fill to the left of it means you are under pace and a fill to the
 * right means you will run out early. Both are computed in UTC because the quota
 * window is UTC.
 *
 * In-flight jobs are drawn as a separate hatched segment because quota counts
 * SUBMISSIONS, not completions (api/quota.py) — those jobs are already spent, and
 * showing them as "not yet used" would overstate what is left.
 */
export function QuotaMeter({
  used,
  limit,
  inFlight = 0,
}: {
  used: number;
  limit: number | null;
  inFlight?: number;
}) {
  const hatchId = useSvgId("hatch");

  if (limit == null) {
    return (
      <div>
        <div className="flex items-baseline justify-between gap-3">
          <span className="font-mono text-2xl leading-none font-semibold text-ink tabular-nums">
            {n(used)}
          </span>
          <span className="font-mono text-xs text-faint">unlimited</span>
        </div>
        <div className="mt-3 h-3 w-full rounded-pill bg-surface-2">
          <div className="h-full w-full rounded-pill bg-grad-brand opacity-25" />
        </div>
        <p className="mt-2 text-xs text-muted">
          Jobs submitted this month. This plan has no monthly ceiling.
        </p>
      </div>
    );
  }

  const pct = limit > 0 ? Math.min(1, used / limit) : 0;
  // `used` already includes in-flight submissions, so the hatched part is carved out
  // of the fill rather than added on top of it.
  const settled = Math.max(0, used - inFlight);
  const settledPct = limit > 0 ? Math.min(1, settled / limit) : 0;
  const pace = monthProgress();
  const remaining = Math.max(0, limit - used);
  const overPace = pct > pace + 0.05;

  return (
    <div>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="font-mono text-2xl leading-none font-semibold text-ink tabular-nums">
          {n(used)}
          <span className="text-base text-faint"> / {n(limit)}</span>
        </span>
        <span
          className={`font-mono text-xs tabular-nums ${
            remaining === 0 ? "text-danger" : overPace ? "text-ink-dim" : "text-ok"
          }`}
        >
          {remaining === 0 ? "none left" : `${n(remaining)} left`}
        </span>
      </div>

      <div className="relative mt-3 h-3 w-full overflow-hidden rounded-pill bg-surface-2">
        {/* settled usage */}
        <div
          className="absolute inset-y-0 left-0 bg-grad-brand"
          style={{ width: `${settledPct * 100}%` }}
        />
        {/* In flight, hatched. The pattern is defined INSIDE the svg that uses it —
            referencing a paint server from a different svg element in the document
            works in most engines and is not worth relying on. */}
        {inFlight > 0 && (
          <svg
            className="absolute inset-y-0 h-full"
            style={{ left: `${settledPct * 100}%`, width: `${(pct - settledPct) * 100}%` }}
            preserveAspectRatio="none"
            aria-hidden
          >
            <defs>
              <pattern
                id={hatchId}
                width="6"
                height="6"
                patternUnits="userSpaceOnUse"
                patternTransform="rotate(45)"
              >
                <rect width="6" height="6" fill="var(--color-accent)" opacity="0.25" />
                <line x1="0" y1="0" x2="0" y2="6" stroke="var(--color-accent)" strokeWidth="2.5" />
              </pattern>
            </defs>
            <rect width="100%" height="100%" fill={`url(#${hatchId})`} />
          </svg>
        )}
        {/* the pace marker */}
        <div
          className="absolute inset-y-0 w-px bg-ink/70"
          style={{ left: `${pace * 100}%` }}
          aria-hidden
        />
      </div>

      <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
        <span>
          <span className="mr-1 inline-block h-2 w-2 rounded-sm bg-ink/70 align-middle" />
          {Math.round(pace * 100)}% through the month
        </span>
        {inFlight > 0 && <span>{inFlight} in flight, already counted</span>}
        <span className={overPace ? "text-ink-dim" : "text-ok"}>
          {overPace ? "ahead of an even burn" : "within an even burn"}
        </span>
      </p>
    </div>
  );
}

/* ----------------------------------------------------------------- columns */

type Series = "jobs" | "gpu_seconds";

/**
 * Daily activity. Columns, not a line: the data is a count per discrete day, and a
 * line implies values between the days that do not exist.
 *
 * Rendered in a viewBox with `preserveAspectRatio="none"` so it fills whatever width
 * the card has — but the labels live in HTML rather than SVG text, because non-uniform
 * scaling would stretch the glyphs.
 */
export function ColumnChart({ days }: { days: UsageDay[] }) {
  const [series, setSeries] = useState<Series>("jobs");
  const values = days.map((d) => (series === "jobs" ? d.jobs : d.gpu_seconds));
  const peak = Math.max(1, ...values);
  const W = 100;
  const H = 34;
  const gap = days.length > 40 ? 0.15 : 0.3;
  const slot = W / Math.max(1, days.length);
  const barW = Math.max(0.4, slot * (1 - gap));

  const todayIndex = days.length - 1;
  const label = (v: number) => (series === "jobs" ? n(v) : duration(v));

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <p className="font-mono text-[10px] font-semibold tracking-label uppercase text-faint">
          {days.length} days · peak {label(peak)}
        </p>
        <div className="flex gap-1" role="tablist" aria-label="Series">
          {(["jobs", "gpu_seconds"] as Series[]).map((s) => (
            <button
              key={s}
              role="tab"
              aria-selected={series === s}
              onClick={() => setSeries(s)}
              className={`rounded-chip border px-2 py-0.5 font-mono text-[10px] uppercase tracking-label transition-colors ${
                series === s
                  ? "border-accent bg-accent/10 text-accent"
                  : "border-border text-muted hover:text-ink"
              }`}
            >
              {s === "jobs" ? "jobs" : "gpu"}
            </button>
          ))}
        </div>
      </div>

      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="h-32 w-full"
        role="img"
        aria-label={`Daily ${series === "jobs" ? "jobs" : "GPU seconds"} over ${days.length} days`}
      >
        {/* a faint baseline, so an all-zero window still reads as a chart */}
        <line x1="0" y1={H} x2={W} y2={H} stroke="var(--color-border)" strokeWidth="0.3" />
        {days.map((d, i) => {
          const v = values[i];
          const h = v > 0 ? Math.max(0.6, (v / peak) * (H - 2)) : 0;
          const x = i * slot + (slot - barW) / 2;
          return (
            <g key={d.day}>
              {/* Full-height hit area, so hovering a short column still works. */}
              <rect x={x} y={0} width={barW} height={H} fill="transparent">
                <title>{`${shortDay(d.day)} — ${label(v)}`}</title>
              </rect>
              {h > 0 && (
                <rect
                  x={x}
                  y={H - h}
                  width={barW}
                  height={h}
                  rx={barW > 1.5 ? 0.4 : 0}
                  fill={i === todayIndex ? "var(--color-accent-3)" : "var(--color-accent)"}
                  opacity={i === todayIndex ? 1 : 0.75}
                />
              )}
            </g>
          );
        })}
      </svg>

      <div className="mt-1.5 flex justify-between font-mono text-[10px] text-faint">
        <span>{shortDay(days[0]?.day ?? "")}</span>
        <span>{days.length > 2 ? shortDay(days[Math.floor(days.length / 2)].day) : ""}</span>
        <span>today</span>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------- ring */

/** A ring for one fraction. Used for the concurrency slot count. */
export function DonutRing({
  value,
  max,
  label,
  sub,
}: {
  value: number;
  max: number;
  label: string;
  sub?: string;
}) {
  const r = 30;
  const c = 2 * Math.PI * r;
  const frac = max > 0 ? Math.min(1, value / max) : 0;
  const gradId = useSvgId("ring");
  return (
    <div className="flex items-center gap-4">
      <svg viewBox="0 0 80 80" className="h-20 w-20 flex-none" aria-hidden>
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor="var(--color-accent)" />
            <stop offset="1" stopColor="var(--color-accent-2)" />
          </linearGradient>
        </defs>
        <circle cx="40" cy="40" r={r} fill="none" stroke="var(--color-surface-2)" strokeWidth="8" />
        <circle
          cx="40"
          cy="40"
          r={r}
          fill="none"
          stroke={`url(#${gradId})`}
          strokeWidth="8"
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={c * (1 - frac)}
          transform="rotate(-90 40 40)"
          className="transition-[stroke-dashoffset] duration-500"
        />
      </svg>
      <div className="min-w-0">
        <p className="font-mono text-xl leading-none font-semibold text-ink tabular-nums">
          {value}
          <span className="text-sm text-faint"> / {max}</span>
        </p>
        <p className="mt-1 text-sm text-ink-dim">{label}</p>
        {sub && <p className="mt-0.5 text-xs text-muted">{sub}</p>}
      </div>
    </div>
  );
}
