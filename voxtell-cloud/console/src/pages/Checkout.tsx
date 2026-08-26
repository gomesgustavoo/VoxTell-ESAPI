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

import { Link, useSearchParams } from "react-router-dom";

import { Alert, Card, SectionHeader, buttonClass } from "../components/ui";

const NAMES: Record<string, string> = {
  explorer: "Explorer",
  clinician: "Pro",
  enterprise: "Enterprise",
};

export default function Checkout() {
  const [params] = useSearchParams();
  const plan = params.get("plan") ?? "";
  // Unknown ids are shown as-is rather than silently mapped to a default: a typo in a
  // marketing link should be visible, not quietly sell something else.
  const name = NAMES[plan];

  return (
    <div className="flex flex-col gap-6">
      <SectionHeader label="Checkout" metric="billing not connected">
        {name
          ? `You selected ${name}. Nothing has been charged.`
          : "Nothing has been charged."}
      </SectionHeader>

      <Card title={name ? `${name} — noted` : "Choose a plan"}>
        {!name && plan && (
          <Alert>
            Unrecognised plan “{plan}”. Pick one from the billing page instead.
          </Alert>
        )}

        <p className="text-sm text-ink-dim">
          Self-serve payment is not switched on yet. Your account keeps its current
          allowance in the meantime — nothing stops working, and there is no card on
          file to fail.
        </p>
        <p className="mt-3 text-sm text-muted">
          When checkout opens, this page becomes it, at this exact address. If you want
          to start now, write to us and we will set the plan up on your account
          directly.
        </p>

        <div className="mt-5 flex flex-wrap gap-2">
          <a
            className="inline-flex min-h-10 items-center justify-center rounded-chip bg-grad-brand px-4 text-sm font-semibold text-accent-ink shadow-accent transition-[filter,transform] hover:-translate-y-px hover:brightness-110"
            href={`mailto:support@dicomsegvr.com?subject=${encodeURIComponent(
              name ? `VoxTell ${name} plan` : "VoxTell plan",
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
  );
}
