// Billing.
//
// Honest about what exists. Plans, subscriptions and Stripe are not wired yet, so
// this shows what /v1/me actually knows — the quota an operator set — and says plainly
// that billing is not connected, rather than rendering an empty plan card that implies
// a subscription system is present and broken.
//
// When Phase E/F land, `plan` and `subscription` arrive on /v1/me additively and this
// page grows a real plan card plus a portal link. The layout is built for that.

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api } from "../lib/api";
import { daysLeftInMonth, n } from "../lib/format";
import { useAuth } from "../auth/AuthProvider";
import { QuotaMeter } from "../components/charts";
import { Alert, Card, SectionHeader, Skeleton, StatCard } from "../components/ui";

const PLANS = [
  {
    id: "explorer",
    name: "Explorer",
    price: "$12.99",
    jobs: "60 jobs a month",
    keys: "2 workstation keys",
    bundle: "DicomSegVR Explorer",
  },
  {
    id: "clinician",
    name: "Pro",
    price: "$49.99",
    jobs: "250 jobs a month",
    keys: "5 workstation keys",
    bundle: "DicomSegVR Pro",
    featured: true,
  },
  {
    id: "enterprise",
    name: "Enterprise",
    price: "$199.99",
    jobs: "Unlimited jobs",
    keys: "Unlimited keys",
    bundle: "DicomSegVR Enterprise",
  },
];

export default function Billing() {
  const { token } = useAuth();
  const me = useQuery({
    queryKey: ["me"],
    queryFn: () => api.me(token!),
    enabled: !!token,
    staleTime: 30_000,
  });

  const inFlight = (me.data?.queued ?? 0) + (me.data?.running ?? 0);

  return (
    <div className="flex flex-col gap-6">
      <SectionHeader label="Billing" metric={`${daysLeftInMonth()} days to the reset`}>
        Your quota and what each plan includes. Every plan bundles the matching
        DicomSegVR tier on the same subscription.
      </SectionHeader>

      <Alert tone="info">
        Self-serve billing is not connected yet. Your account is on an operator-set
        quota, and nothing will be charged. Choosing a plan below opens a page that
        records your interest.
      </Alert>

      {me.isError && <Alert>Could not load your account: {(me.error as Error).message}</Alert>}

      <div className="grid gap-4 lg:grid-cols-[1.4fr_1fr]">
        <Card title="Current allowance">
          {me.isLoading ? (
            <Skeleton className="h-16 w-full" />
          ) : me.data ? (
            <QuotaMeter
              used={me.data.used_this_month}
              limit={me.data.monthly_quota}
              inFlight={inFlight}
            />
          ) : null}
        </Card>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-1">
          <StatCard
            label="Concurrent cap"
            value={me.data ? me.data.max_outstanding : "—"}
            unit="jobs"
            hint="Queued plus running at once."
          />
          <StatCard
            label="Used this month"
            value={me.data ? n(me.data.used_this_month) : "—"}
            hint="Submissions, counted from the 1st (UTC)."
          />
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        {PLANS.map((p) => (
          <div
            key={p.id}
            className={
              "relative flex flex-col rounded-card border bg-surface p-5 " +
              (p.featured
                ? "border-transparent shadow-accent [background:linear-gradient(var(--color-surface),var(--color-surface))_padding-box,var(--vx-grad-brand)_border-box]"
                : "border-border")
            }
          >
            {p.featured && (
              <span className="absolute -top-2.5 left-1/2 -translate-x-1/2 rounded-pill bg-accent-2 px-2.5 py-0.5 text-[10px] font-semibold tracking-wide uppercase text-white">
                Most clinics
              </span>
            )}
            <h3 className="text-lg font-bold text-ink">{p.name}</h3>
            <p className="mt-1 flex items-baseline gap-1.5">
              <span className="text-xl font-bold tracking-tight text-ink tabular-nums">
                {p.price}
              </span>
              <span className="font-mono text-xs text-faint">/ month</span>
            </p>
            <ul className="mt-4 flex-1 space-y-2 text-sm text-ink-dim">
              <li>{p.jobs}</li>
              <li>{p.keys}</li>
              <li>Up to 16 structures per job</li>
            </ul>
            <p className="mt-3 font-mono text-xs text-faint">
              + includes <span className="text-muted">{p.bundle}</span>
            </p>
            <Link
              to={`/checkout?plan=${p.id}`}
              className={
                "mt-4 inline-flex min-h-10 items-center justify-center rounded-chip px-4 text-sm font-semibold transition-[filter,transform] hover:-translate-y-px " +
                (p.featured
                  ? "bg-grad-brand text-accent-ink shadow-accent hover:brightness-110"
                  : "border border-border font-mono text-xs uppercase tracking-label text-ink hover:border-accent hover:text-accent")
              }
            >
              Choose {p.name}
            </Link>
          </div>
        ))}
      </div>
    </div>
  );
}
