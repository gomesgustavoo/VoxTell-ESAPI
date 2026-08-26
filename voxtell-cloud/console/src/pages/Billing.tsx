// Billing.
//
// Honest about what exists. The API has no plan model at all — one flat
// VOXTELL_DEFAULT_MONTHLY_QUOTA, no subscriptions, no Stripe — so this shows what
// /v1/me actually knows (the quota an operator set) and says plainly that billing is
// not connected, rather than rendering an empty plan card that implies a subscription
// system is present and broken.
//
// When `plan` and `subscription` arrive on /v1/me additively, this page grows a real
// current-plan marker (PlanCard already takes `current`) plus a portal link. The
// layout is built for that.
//
// The plan table itself lives in lib/plans.ts and is shared with Checkout. It used to
// be duplicated here, and the copy had already drifted from the marketing page.

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api } from "../lib/api";
import { PLANS, TRIAL_DAYS } from "../lib/plans";
import { daysLeftInMonth, n } from "../lib/format";
import { useAuth } from "../auth/AuthProvider";
import { QuotaMeter } from "../components/charts";
import {
  Alert,
  Card,
  PlanCard,
  SectionHeader,
  Skeleton,
  StatCard,
  buttonClass,
} from "../components/ui";

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
        quota and nothing will be charged. Choosing a plan below records your interest.
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
          ) : (
            // Not `null`. An empty card reads as a broken widget; say which of the
            // two things happened, because they need different actions.
            <p className="text-sm text-muted">
              {me.isError
                ? "Your allowance could not be read. The figures below are the published plans, not your account."
                : "No allowance on file for this account yet."}
            </p>
          )}
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
          <PlanCard
            key={p.id}
            plan={p}
            cta={
              p.checkout ? (
                <Link
                  to={p.checkout}
                  className={buttonClass(p.featured ? "primary" : "ghost", "md")}
                >
                  Choose {p.name}
                </Link>
              ) : (
                <Link to="/checkout?plan=explorer" className={buttonClass("ghost", "md")}>
                  Start {TRIAL_DAYS}-day trial
                </Link>
              )
            }
          />
        ))}
      </div>
    </div>
  );
}
