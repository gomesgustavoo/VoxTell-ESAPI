// The plan table, in one place.
//
// WHY THIS FILE EXISTS. Billing.tsx and Checkout.tsx each carried their own copy of
// the prices, and Billing's copy had already drifted from the marketing page: the
// console listed "Up to 16 structures per job" for all three tiers while
// landing/index.html sells priority queue placement, concurrency and CT/MR/PET. Two
// hand-maintained price lists in one product is how DicomSegVR ended up with three.
//
// THIS IS NOT THE SOURCE OF TRUTH FOR MONEY. The amounts live on live Stripe Price
// objects shared with DicomSegVR, and a Price's amount is immutable in Stripe — so
// changing a number here is not a copy edit, it needs a new Price first. The
// authority for the strings is tests/test_landing_prices.py::PLANS, which pins the
// landing page's cards against its JSON-LD; keep this table equal to that one.
//
// When /v1/plans exists (the API has no plan model at all today — one flat
// VOXTELL_DEFAULT_MONTHLY_QUOTA), this becomes a fallback for a fetch and the
// duplication finally goes away.

export interface Plan {
  /** Stays `clinician` for the middle tier: that is what DicomSegVR's Stripe
   *  metadata and the existing checkout URLs already use. `name` is what a
   *  customer reads. */
  id: "explorer" | "clinician" | "enterprise";
  name: string;
  price: string;
  features: string[];
  bundle: string;
  featured?: boolean;
  /** The trial needs no checkout, so Explorer has none. */
  checkout: string | null;
}

export const TRIAL_DAYS = 14;

export const PLANS: Plan[] = [
  {
    id: "explorer",
    name: "Explorer",
    price: "$12.99",
    features: [
      "60 jobs a month",
      "Up to 16 structures per job",
      "One segmentation at a time",
      "2 workstation keys",
      "CT, MR and PET",
    ],
    bundle: "DicomSegVR Explorer",
    checkout: null,
  },
  {
    id: "clinician",
    name: "Pro",
    price: "$49.99",
    features: [
      "Everything in Explorer",
      "250 jobs a month",
      "Priority queue placement",
      "5 workstation keys",
      "Priority support",
    ],
    bundle: "DicomSegVR Pro",
    featured: true,
    checkout: "/checkout?plan=clinician",
  },
  {
    id: "enterprise",
    name: "Enterprise",
    price: "$199.99",
    features: [
      "Everything in Pro",
      "Unlimited jobs",
      "2 concurrent segmentations",
      "Unlimited workstation keys",
      "Top-priority queue · dedicated support",
    ],
    bundle: "DicomSegVR Enterprise",
    checkout: "/checkout?plan=enterprise",
  },
];

export function planById(id: string | null): Plan | undefined {
  return PLANS.find((p) => p.id === id);
}
