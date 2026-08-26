// Formatting, in one place.
//
// Centralised because these are the values a physicist reads off a screen and
// compares against a plan: a duration rendered two ways in two panels reads as two
// different measurements. Every numeric output here is intended for a `tabular-nums`
// context so columns line up.

/** 1234 -> "1,234". Always en-US so the thousands separator cannot depend on the workstation locale. */
export function n(value: number | null | undefined): string {
  if (value == null) return "—";
  return value.toLocaleString("en-US");
}

/** Seconds -> "42 s" / "3m 12s" / "1h 04m". Never a bare decimal count of seconds. */
export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 1) return "<1 s";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m < 60) return `${m}m ${String(s).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}

/** GPU seconds are quoted in minutes once they pass an hour of cumulative time. */
export function gpuTime(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 90) return `${seconds.toFixed(1)} s`;
  const m = seconds / 60;
  if (m < 90) return `${m.toFixed(1)} min`;
  return `${(m / 60).toFixed(1)} h`;
}

const UNITS = ["B", "KB", "MB", "GB", "TB"];

export function bytes(value: number | null | undefined): string {
  if (value == null) return "—";
  let v = value;
  let i = 0;
  while (v >= 1024 && i < UNITS.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${UNITS[i]}`;
}

/** Voxels are always large; 5,242,880 -> "5.2 M". */
export function voxels(value: number | null | undefined): string {
  if (value == null) return "—";
  if (value < 1000) return String(value);
  if (value < 1e6) return `${(value / 1e3).toFixed(0)} K`;
  return `${(value / 1e6).toFixed(1)} M`;
}

/**
 * An absolute local timestamp. Deliberately NOT relative ("2 hours ago") in the
 * job table: a planner correlating a job with a plan needs the clock time, and a
 * relative label forces a hover to find it. `relative()` exists for the places
 * where recency is the point.
 */
export function stamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("en-GB", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function relative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (secs < 45) return "just now";
  if (secs < 5400) return `${Math.round(secs / 60)} min ago`;
  if (secs < 172800) return `${Math.round(secs / 3600)} h ago`;
  return `${Math.round(secs / 86400)} d ago`;
}

/** "2026-08-12" -> "12 Aug". For chart axes, where space is the constraint. */
export function shortDay(isoDate: string): string {
  const [y, m, d] = isoDate.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  });
}

/**
 * Which day of the monthly cycle we are on, 0..1.
 *
 * Drives the quota meter's pace marker. Computed in UTC because the quota window
 * itself is UTC (`quota.month_start()`), so a browser in UTC+13 must not be told it
 * is a day further through the month than the server thinks.
 */
export function monthProgress(now = new Date()): number {
  const y = now.getUTCFullYear();
  const m = now.getUTCMonth();
  const start = Date.UTC(y, m, 1);
  const end = Date.UTC(m === 11 ? y + 1 : y, (m + 1) % 12, 1);
  return Math.min(1, Math.max(0, (now.getTime() - start) / (end - start)));
}

export function daysLeftInMonth(now = new Date()): number {
  const end = Date.UTC(
    now.getUTCMonth() === 11 ? now.getUTCFullYear() + 1 : now.getUTCFullYear(),
    (now.getUTCMonth() + 1) % 12,
    1,
  );
  return Math.max(0, Math.ceil((end - now.getTime()) / 86400000));
}
