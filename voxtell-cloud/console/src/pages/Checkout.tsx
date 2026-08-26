// The checkout placeholder.
//
// This route exists so the landing page's pricing CTAs are not dead links. They point
// at /dashboard/checkout?plan=… and before this they fell through the SPA catch-all
// to the sign-in shell — a customer clicking "Choose Pro" got a sign-in box with no
// explanation.
//
// It is deliberately a real page rather than a redirect to Billing: the URL is what
// the marketing page commits to, so when Stripe lands the same URL becomes the real
// checkout and nothing published has to change.
//
// It now echoes the plan it was sent, from lib/plans.ts. Sending somebody to a page
// that names their plan and shows nothing about it is the part that reads as broken;
// a customer who clicked "Choose Pro" should see Pro.

import { Link, useSearchParams } from "react-router-dom";

import { PLANS, planById } from "../lib/plans";
import { Alert, Card, PlanCard, SectionHeader, buttonClass } from "../components/ui";

export default function Checkout() {
  const [params] = useSearchParams();
  const requested = params.get("plan") ?? "";
  // Unknown ids are shown as-is rather than silently mapped to a default: a typo in a
  // marketing link should be visible, not quietly sell something else.
  const plan = planById(requested);

  return (
    <div className="flex flex-col gap-6">
      <SectionHeader label="Checkout" metric="billing not connected">
        {plan
          ? `You selected ${plan.name}. Nothing has been charged.`
          : "Nothing has been charged."}
      </SectionHeader>

      {!plan && requested && (
        <Alert>
          Unrecognised plan “{requested}”. Pick one below, or from the billing page.
        </Alert>
      )}

      <div className="grid gap-4 lg:grid-cols-[1fr_1.3fr] lg:items-start">
        {/* The plan it was actually asked for, priced. If the link carried no plan
            or a bad one, show all three rather than an empty column. */}
        <div className="grid gap-4">
          {(plan ? [plan] : PLANS).map((p) => (
            <PlanCard key={p.id} plan={p} />
          ))}
        </div>

        <Card title={plan ? `${plan.name} — noted` : "Choose a plan"}>
          <p className="text-sm text-ink-dim">
            Self-serve payment is not switched on yet. Your account keeps its current
            allowance in the meantime — nothing stops working, and there is no card on
            file to fail.
          </p>
          <p className="mt-3 text-sm text-muted">
            When checkout opens, this page becomes it, at this exact address. To start
            now, write to us and we will set the plan up on your account directly.
          </p>

          <div className="mt-5 flex flex-wrap gap-2">
            <a
              className={buttonClass("primary", "md")}
              href={`mailto:support@dicomsegvr.com?subject=${encodeURIComponent(
                plan ? `VoxTell ${plan.name} plan` : "VoxTell plan",
              )}`}
            >
              Email us to start
            </a>
            <Link to="/billing" className={buttonClass("ghost", "md")}>
              Back to billing
            </Link>
          </div>
        </Card>
      </div>
    </div>
  );
}
