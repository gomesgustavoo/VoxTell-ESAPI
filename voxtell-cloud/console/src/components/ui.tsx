import type { ButtonHTMLAttributes, ReactNode } from "react";

export function Button({
  variant = "primary",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost" | "danger";
}) {
  const base =
    "inline-flex items-center justify-center gap-2 rounded-lg px-3.5 py-2 text-sm font-medium " +
    "transition-colors disabled:cursor-not-allowed disabled:opacity-50 " +
    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";
  const variants = {
    primary: "bg-accent text-accent-ink hover:bg-accent/85",
    ghost: "border border-border text-ink hover:bg-surface-2",
    danger: "border border-danger/40 text-danger hover:bg-danger/10",
  } as const;
  return <button className={`${base} ${variants[variant]} ${className}`} {...props} />;
}

export function Card({
  title,
  action,
  children,
}: {
  title: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-xl border border-border bg-surface/70 backdrop-blur-sm">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-5 py-3.5">
        <h2 className="text-sm font-semibold tracking-wide uppercase text-muted">
          {title}
        </h2>
        {action}
      </header>
      <div className="p-5">{children}</div>
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-sm text-muted">
      {children}
    </p>
  );
}

export function Alert({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
      {children}
    </p>
  );
}

const STATE_STYLES: Record<string, string> = {
  awaiting_upload: "border-border text-muted",
  queued: "border-warn/40 text-warn",
  running: "border-accent/50 text-accent",
  done: "border-accent/40 text-accent",
  failed: "border-danger/40 text-danger",
  cancelled: "border-border text-muted",
  expired: "border-border text-muted",
};

export function StateBadge({ state }: { state: string }) {
  return (
    <span
      className={`inline-flex rounded-md border px-2 py-0.5 text-xs font-medium ${
        STATE_STYLES[state] ?? "border-border text-muted"
      }`}
    >
      {state.replace("_", " ")}
    </span>
  );
}

export function Progress({ value }: { value: number }) {
  return (
    <div
      className="h-1.5 w-full overflow-hidden rounded-full bg-surface-2"
      role="progressbar"
      aria-valuenow={Math.round(value * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div
        className="h-full rounded-full bg-accent transition-[width] duration-500"
        style={{ width: `${Math.max(2, value * 100)}%` }}
      />
    </div>
  );
}
