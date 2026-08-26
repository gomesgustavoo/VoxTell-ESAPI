// The UI primitives.
//
// This file replaced five components — Button, Card, Empty, Alert, StateBadge,
// Progress — that between them left Table, Input, Select, Tabs, Modal and Toast to
// be hand-rolled per page, in slightly different ways each time. Everything here is
// styled from the generated design tokens, so the console and the landing page
// cannot drift apart: change design/tokens.css and both move.

import { useEffect, useRef, useState } from "react";
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from "react";

import type { JobState } from "../lib/api";
import { structureColour } from "../lib/structures";

/* -- buttons ------------------------------------------------------------- */

type Variant = "primary" | "ghost" | "danger" | "subtle";
type Size = "sm" | "md" | "lg";

const BTN_BASE =
  "inline-flex items-center justify-center gap-2 rounded-chip font-semibold " +
  "transition-[filter,transform,border-color,color,background-color] " +
  "hover:-translate-y-px active:translate-y-0 " +
  "disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:translate-y-0 " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";

const BTN_SIZES: Record<Size, string> = {
  sm: "px-3 py-1.5 text-xs min-h-8",
  md: "px-4 py-2 text-sm min-h-10",
  lg: "px-5 py-2.5 text-base min-h-12",
};

const BTN_VARIANTS: Record<Variant, string> = {
  // The brand ramp, with near-black ink — the same primary the landing page uses.
  primary: "bg-grad-brand text-accent-ink shadow-accent hover:brightness-110",
  // Mono, uppercase, tracked outline: DicomSegVR's secondary, and the thing that
  // makes a page in this family recognisable at a glance.
  ghost:
    "border border-border text-ink font-mono uppercase tracking-label text-xs " +
    "hover:border-accent hover:text-accent",
  danger: "border border-danger/40 text-danger hover:bg-danger/10",
  subtle: "text-muted hover:text-ink hover:bg-surface-2",
};

/**
 * Button styling as a class string, for elements that must not be a `<button>`.
 *
 * Exported because the alternative people reach for is `<Button><Link/></Button>`,
 * and a `<button>` wrapping an `<a>` is invalid HTML: the click lands on the button,
 * the anchor never navigates, and keyboard users get two conflicting roles on one
 * control. Anything that navigates should be a link that *looks* like a button.
 */
export function buttonClass(variant: Variant = "ghost", size: Size = "md"): string {
  return `${BTN_BASE} ${BTN_SIZES[size]} ${BTN_VARIANTS[variant]}`;
}

export function Button({
  variant = "ghost",
  size = "md",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  size?: Size;
}) {
  return <button className={`${buttonClass(variant, size)} ${className}`} {...props} />;
}

export function CopyButton({ value, label = "Copy" }: { value: string; label?: string }) {
  const [done, setDone] = useState(false);
  useEffect(() => {
    if (!done) return;
    const t = setTimeout(() => setDone(false), 1600);
    return () => clearTimeout(t);
  }, [done]);
  return (
    <Button
      size="sm"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setDone(true);
        } catch {
          // Clipboard access can be refused outright on a locked-down workstation.
          // Say so rather than silently doing nothing — the user needs to know to
          // select the text by hand.
          window.prompt("Copy this manually:", value);
        }
      }}
    >
      {done ? "Copied" : label}
    </Button>
  );
}

/* -- surfaces ------------------------------------------------------------ */

export function Card({
  title,
  eyebrow,
  action,
  children,
  className = "",
  padded = true,
}: {
  title?: string;
  eyebrow?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  padded?: boolean;
}) {
  return (
    <section className={`rounded-card border border-border bg-surface ${className}`}>
      {(title || action) && (
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-5 py-3.5">
          <div className="min-w-0">
            {eyebrow && (
              <p className="font-mono text-[10px] font-semibold tracking-eyebrow uppercase text-accent">
                {eyebrow}
              </p>
            )}
            {title && <h2 className="truncate text-md font-semibold text-ink">{title}</h2>}
          </div>
          {action}
        </header>
      )}
      <div className={padded ? "p-5" : ""}>{children}</div>
    </section>
  );
}

/** The section device from the landing page: a tick, a mono label, a real figure. */
export function SectionHeader({
  label,
  metric,
  children,
}: {
  label: string;
  metric?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="mb-5">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 border-b border-border pb-2.5">
        <span className="flex min-w-0 items-center gap-2.5 font-mono text-xs font-semibold tracking-eyebrow uppercase whitespace-nowrap text-accent">
          <span className="h-px w-[22px] flex-none bg-accent opacity-85" />
          {label}
        </span>
        {metric && (
          <span className="ml-auto min-w-0 font-mono text-xs whitespace-nowrap text-faint tabular-nums">
            {metric}
          </span>
        )}
      </div>
      {children && <p className="mt-3 max-w-[62ch] text-sm text-muted">{children}</p>}
    </div>
  );
}

export function StatCard({
  label,
  value,
  unit,
  hint,
  tone = "ink",
}: {
  label: string;
  value: ReactNode;
  unit?: string;
  hint?: ReactNode;
  tone?: "ink" | "accent" | "ok" | "danger" | "muted";
}) {
  const tones = {
    ink: "text-ink",
    accent: "text-accent",
    ok: "text-ok",
    danger: "text-danger",
    muted: "text-muted",
  } as const;
  return (
    <div className="rounded-card border border-border bg-surface px-4 py-3.5">
      <p className="font-mono text-[10px] font-semibold tracking-label uppercase text-faint">
        {label}
      </p>
      <p className="mt-1.5 flex items-baseline gap-1.5">
        <span className={`font-mono text-2xl leading-none font-semibold tabular-nums ${tones[tone]}`}>
          {value}
        </span>
        {unit && <span className="font-mono text-xs text-faint">{unit}</span>}
      </p>
      {hint && <p className="mt-1.5 text-xs leading-snug text-muted">{hint}</p>}
    </div>
  );
}

export function Empty({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="rounded-card border border-dashed border-border px-4 py-10 text-center">
      <p className="text-sm text-muted">{children}</p>
      {action && <div className="mt-4 flex justify-center">{action}</div>}
    </div>
  );
}

export function Alert({
  children,
  tone = "danger",
}: {
  children: ReactNode;
  tone?: "danger" | "info" | "ok";
}) {
  const tones = {
    danger: "border-danger/40 bg-danger/10 text-danger",
    info: "border-accent/40 bg-accent/8 text-accent-3",
    ok: "border-ok/40 bg-ok/10 text-ok",
  } as const;
  return (
    <p className={`rounded-chip border px-3 py-2 text-sm ${tones[tone]}`} role="status">
      {children}
    </p>
  );
}

export function Skeleton({ className = "h-4 w-full" }: { className?: string }) {
  return <span className={`block animate-pulse rounded bg-surface-2 ${className}`} aria-hidden />;
}

/* -- job state ----------------------------------------------------------- */

// No amber anywhere. `queued` used to be amber, which made a waiting job look like a
// warning; it is slate with a pulsing dot, which is what waiting actually looks like.
const STATE_STYLES: Record<JobState, string> = {
  awaiting_upload: "border-border text-faint",
  queued: "border-border text-muted",
  running: "border-accent/50 text-accent",
  done: "border-ok/40 text-ok",
  failed: "border-danger/40 text-danger",
  cancelled: "border-border text-faint",
  expired: "border-border text-faint",
};

export function StateBadge({ state }: { state: JobState }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-chip border px-2 py-0.5 font-mono text-[11px] whitespace-nowrap ${
        STATE_STYLES[state] ?? "border-border text-muted"
      }`}
    >
      {state === "running" && (
        <span className="h-1.5 w-1.5 flex-none rounded-full bg-accent" />
      )}
      {state === "queued" && (
        <span className="h-1.5 w-1.5 flex-none animate-pulse rounded-full bg-muted" />
      )}
      {state.replace("_", " ")}
    </span>
  );
}

/** A prompt, coloured by the structure it names — the ledger device, inline. */
export function PromptChip({ prompt }: { prompt: string }) {
  const colour = structureColour(prompt);
  return (
    <span className="inline-flex items-center gap-1.5 rounded-chip border border-border bg-surface-2 px-2 py-0.5 font-mono text-[11px] text-ink-dim">
      <span
        className="h-2 w-2 flex-none rounded-sm"
        style={{ background: colour ?? "var(--color-faint)" }}
        aria-hidden
      />
      {prompt}
    </span>
  );
}

export function Progress({
  value,
  label,
  tone = "accent",
}: {
  value: number;
  label?: string;
  tone?: "accent" | "ok" | "danger";
}) {
  const pct = Math.round(Math.min(1, Math.max(0, value)) * 100);
  const fills = {
    accent: "bg-grad-brand",
    ok: "bg-ok",
    danger: "bg-danger",
  } as const;
  return (
    <div>
      {label && (
        <div className="mb-1.5 flex items-baseline justify-between gap-2">
          <span className="text-xs text-muted">{label}</span>
          <span className="font-mono text-xs text-ink-dim tabular-nums">{pct}%</span>
        </div>
      )}
      <div
        className="h-2 w-full overflow-hidden rounded-pill bg-surface-2"
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div
          className={`h-full rounded-pill transition-[width] duration-500 ${fills[tone]}`}
          // A visible sliver at 0 so a just-claimed job reads as started rather
          // than as an empty track.
          style={{ width: `${Math.max(2, pct)}%` }}
        />
      </div>
    </div>
  );
}

/* -- form controls ------------------------------------------------------- */

const FIELD =
  "w-full rounded-chip border border-border bg-ground px-3 py-2 text-sm text-ink " +
  "placeholder:text-faint focus:border-accent focus:outline-none " +
  "focus:ring-2 focus:ring-accent/25 disabled:opacity-50";

export function Input({ className = "", ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={`${FIELD} ${className}`} {...props} />;
}

export function Select({
  className = "",
  children,
  ...props
}: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={`${FIELD} ${className}`} {...props}>
      {children}
    </select>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1.5 block font-mono text-[10px] font-semibold tracking-label uppercase text-faint">
        {label}
      </span>
      {children}
      {hint && <span className="mt-1 block text-xs text-muted">{hint}</span>}
    </label>
  );
}

/* -- table --------------------------------------------------------------- */

export function Table({ head, children }: { head: ReactNode[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[36rem] border-collapse text-sm">
        <thead>
          <tr className="border-b border-border">
            {head.map((h, i) => (
              <th
                key={i}
                className="px-4 py-2.5 text-left font-mono text-[10px] font-semibold tracking-label uppercase text-faint"
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

export function Row({ children }: { children: ReactNode }) {
  return (
    <tr className="border-b border-border-soft last:border-0 hover:bg-surface-2/50">{children}</tr>
  );
}

export function Cell({
  children,
  className = "",
  mono = false,
}: {
  children: ReactNode;
  className?: string;
  mono?: boolean;
}) {
  return (
    <td className={`px-4 py-3 align-top ${mono ? "font-mono text-xs tabular-nums" : ""} ${className}`}>
      {children}
    </td>
  );
}

/* -- modal --------------------------------------------------------------- */

export function Modal({
  open,
  title,
  onClose,
  children,
  footer,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    // Focus the panel so the keyboard lands inside the dialog rather than staying
    // on whatever opened it.
    panel.current?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ground/70 p-4 backdrop-blur-sm"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={panel}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="w-full max-w-lg rounded-panel border border-border bg-surface shadow-panel outline-none"
      >
        <header className="border-b border-border px-5 py-3.5">
          <h2 className="text-md font-semibold text-ink">{title}</h2>
        </header>
        <div className="p-5">{children}</div>
        {footer && (
          <footer className="flex flex-wrap justify-end gap-2 border-t border-border px-5 py-3.5">
            {footer}
          </footer>
        )}
      </div>
    </div>
  );
}

/* -- toast --------------------------------------------------------------- */

export interface Toast {
  id: number;
  message: string;
  tone: "ok" | "danger";
}

export function ToastStack({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }) {
  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-4 z-50 flex flex-col items-center gap-2 px-4">
      {toasts.map((t) => (
        <button
          key={t.id}
          onClick={() => onDismiss(t.id)}
          className={`pointer-events-auto max-w-md rounded-chip border px-4 py-2 text-sm shadow-panel ${
            t.tone === "ok"
              ? "border-ok/40 bg-surface text-ok"
              : "border-danger/40 bg-surface text-danger"
          }`}
        >
          {t.message}
        </button>
      ))}
    </div>
  );
}

/** Toast state, so pages do not each invent their own. */
export function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const next = useRef(1);
  function push(message: string, tone: Toast["tone"] = "ok") {
    const id = next.current++;
    setToasts((t) => [...t, { id, message, tone }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 5000);
  }
  function dismiss(id: number) {
    setToasts((t) => t.filter((x) => x.id !== id));
  }
  return { toasts, push, dismiss };
}
