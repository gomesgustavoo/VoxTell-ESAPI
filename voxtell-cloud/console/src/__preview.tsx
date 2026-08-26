// The preview harness.
//
// WHY IT EXISTS. Every page in this app sits behind the Keycloak gate in App.tsx, so
// without a live token from https://auth.dicomsegvr.com there is no way to LOOK at
// Overview, Jobs, Keys, Billing or a job detail — not in `npm run dev`, not in
// `vite preview`. The last redesign was reviewed by server-rendering components in
// Node instead, which renders markup but does not run Tailwind, and that is exactly
// why the @theme pruning bug shipped: 22 of 23 structure colours were absent from the
// built CSS and every ledger mark stroked nothing. Markup was correct; resolved CSS
// was not.
//
// This is a second Vite entry (see vite.config.ts, which already does the same for
// silent-renew.html), so it is built with the REAL index.css and the real Tailwind
// pass. Bugs of that class are visible here.
//
// It is not shipped: Dockerfile.console builds with `--mode production` and the
// preview input is excluded there — see the config. Fixtures are deliberately awkward
// (a failed job, a revoked key, an over-pace quota, an unlimited plan) because the
// happy path is the one case that always got looked at.
//
//   npm run preview:harness     →  http://localhost:5174/dashboard/__preview.html

import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import type { ApiKey, Job, Me, SystemState, Usage, Volume } from "./lib/api";
import { PLANS } from "./lib/plans";
import { AppShell } from "./components/layout";
import { JobCard, JobLine, JobTableRow } from "./components/JobRow";
import { ColumnChart, DonutRing, QuotaMeter } from "./components/charts";
import {
  Alert,
  Button,
  Card,
  Empty,
  Field,
  Input,
  Modal,
  PlanCard,
  Progress,
  PromptChip,
  Row,
  Cell,
  SectionHeader,
  Select,
  Skeleton,
  Spinner,
  StatCard,
  StateBadge,
  Table,
  ToastStack,
  buttonClass,
  useToasts,
} from "./components/ui";
import { JOB_STATES } from "./lib/api";
import "./index.css";

/* -- fixtures ------------------------------------------------------------ */

const ME: Me = {
  id: "3f2a7c11-0000-4000-8000-000000000001",
  email: "planner@example-clinic.org",
  username: "planner",
  monthly_quota: 250,
  // Deliberately ahead of pace: on the 26th of a 31-day month the pace marker sits
  // at ~84%, and 214/250 is 86% — so the meter shows the over-pace case rather than
  // the flattering one.
  used_this_month: 214,
  outstanding: 3,
  max_outstanding: 6,
  capabilities: ["volumes"],
  volume_ttl_minutes: 120,
  queued: 2,
  running: 1,
  remaining: 36,
};

const ME_UNLIMITED: Me = { ...ME, monthly_quota: null, used_this_month: 981, remaining: null };

const SYSTEM: SystemState = {
  queue_depth: 4,
  running: 1,
  worker_online: true,
  estimated_wait_seconds: 96,
  snapshot_age_seconds: 3,
};

const SYSTEM_DOWN: SystemState = { ...SYSTEM, worker_online: false, running: 0, queue_depth: 11 };

function makeUsage(days = 28): Usage {
  // A deterministic, uneven series — a flat or random one hides both the axis
  // labelling and the zero-day gaps the chart has to draw.
  const out = [];
  const base = Date.UTC(2026, 7, 26) - (days - 1) * 86_400_000;
  const pattern = [0, 0, 6, 11, 4, 9, 14, 0, 3, 21, 17, 8, 0, 0, 2, 13, 19, 6, 11, 0, 0, 7, 24, 16, 9, 12, 5, 18];
  for (let i = 0; i < days; i++) {
    const jobs = pattern[i % pattern.length];
    out.push({
      day: new Date(base + i * 86_400_000).toISOString().slice(0, 10),
      jobs,
      prompts: jobs * 4,
      gpu_seconds: jobs * 11.4,
      voxels: jobs * 54_000_000,
    });
  }
  return {
    days: out,
    window_days: days,
    since: out[0]!.day,
    total_jobs: out.reduce((a, d) => a + d.jobs, 0),
    total_prompts: out.reduce((a, d) => a + d.prompts, 0),
    total_gpu_seconds: out.reduce((a, d) => a + d.gpu_seconds, 0),
  };
}

const iso = (minutesAgo: number) =>
  new Date(Date.UTC(2026, 7, 26, 22, 0, 0) - minutesAgo * 60_000).toISOString();

const JOBS: Job[] = [
  {
    job_id: "0f0a4d21-7c33-4a90-9b11-000000000001",
    state: "running",
    progress: 0.42,
    message: "segmenting (3 of 7 prompts)",
    error: null,
    prompts: ["liver", "spleen", "right kidney", "aorta", "spinal cord", "stomach", "l1 vertebra"],
    queue_position: null,
    poll_after: 3,
    has_mask: false,
    created_at: iso(4),
    started_at: iso(2),
    finished_at: null,
    queued_at: iso(4),
    duration_seconds: null,
    gpu_seconds: null,
    voxels: null,
    bytes_in: 48_226_304,
    attempts: 1,
    failure_class: null,
    volume_id: "b41e7c62-0000-4000-8000-00000000000a",
  },
  {
    job_id: "0f0a4d21-7c33-4a90-9b11-000000000002",
    state: "queued",
    progress: 0,
    message: null,
    error: null,
    prompts: ["both parotid glands"],
    queue_position: 3,
    estimated_wait_seconds: 96,
    poll_after: 5,
    has_mask: false,
    created_at: iso(6),
    started_at: null,
    finished_at: null,
    queued_at: iso(6),
    duration_seconds: null,
    gpu_seconds: null,
    voxels: null,
    bytes_in: 31_457_280,
    attempts: 1,
    failure_class: null,
    volume_id: "b41e7c62-0000-4000-8000-00000000000a",
  },
  {
    job_id: "0f0a4d21-7c33-4a90-9b11-000000000003",
    state: "done",
    progress: 1,
    message: "complete",
    error: null,
    prompts: ["liver", "the FDG-avid nodule"],
    queue_position: null,
    poll_after: 30,
    has_mask: true,
    created_at: iso(64),
    started_at: iso(63),
    finished_at: iso(62),
    queued_at: iso(64),
    duration_seconds: 71.4,
    gpu_seconds: 12.9,
    voxels: 812_345,
    bytes_in: 52_428_800,
    attempts: 1,
    failure_class: null,
    volume_id: null,
  },
  {
    job_id: "0f0a4d21-7c33-4a90-9b11-000000000004",
    state: "failed",
    progress: 0.66,
    message: null,
    // A long, ugly, real-shaped error: short placeholder text hides how this wraps.
    error:
      "CUDA out of memory. Tried to allocate 2.41 GiB (GPU 0; 11.76 GiB total capacity; 9.02 GiB already allocated)",
    prompts: ["whole body skeleton", "every lymph node station in the mediastinum"],
    queue_position: null,
    poll_after: 30,
    has_mask: false,
    created_at: iso(190),
    started_at: iso(188),
    finished_at: iso(186),
    queued_at: iso(190),
    duration_seconds: 118.2,
    gpu_seconds: 96.1,
    voxels: null,
    bytes_in: 1_073_741_824,
    attempts: 3,
    failure_class: "oom",
    volume_id: null,
  },
  {
    job_id: "0f0a4d21-7c33-4a90-9b11-000000000005",
    state: "expired",
    progress: 1,
    message: null,
    error: null,
    prompts: ["oesophagus"],
    queue_position: null,
    poll_after: 30,
    has_mask: false,
    created_at: iso(3_100),
    started_at: iso(3_099),
    finished_at: iso(3_098),
    queued_at: iso(3_100),
    duration_seconds: 44.0,
    gpu_seconds: 6.2,
    voxels: 41_002,
    bytes_in: 40_894_464,
    attempts: 1,
    failure_class: null,
    volume_id: null,
  },
  {
    job_id: "0f0a4d21-7c33-4a90-9b11-000000000006",
    state: "cancelled",
    progress: 0.1,
    message: null,
    error: null,
    prompts: ["left femur"],
    queue_position: null,
    poll_after: 30,
    has_mask: false,
    created_at: iso(400),
    started_at: iso(399),
    finished_at: iso(399),
    queued_at: iso(400),
    duration_seconds: 8.1,
    gpu_seconds: 0,
    voxels: null,
    bytes_in: 20_971_520,
    attempts: 1,
    failure_class: null,
    volume_id: null,
  },
];

const KEYS: ApiKey[] = [
  {
    id: "k1",
    name: "TPS-WS-04 (planning room 2)",
    prefix: "vxt_9fA2",
    created_at: iso(60 * 24 * 12),
    last_used_at: iso(9),
    expires_at: iso(-60 * 24 * 180),
    revoked_at: null,
  },
  {
    id: "k2",
    name: "unattended-batch",
    prefix: "vxt_31Kd",
    created_at: iso(60 * 24 * 40),
    last_used_at: null,
    expires_at: null,
    revoked_at: null,
  },
  {
    id: "k3",
    name: "decommissioned-ws",
    prefix: "vxt_7bQz",
    created_at: iso(60 * 24 * 200),
    last_used_at: iso(60 * 24 * 150),
    expires_at: null,
    revoked_at: iso(60 * 24 * 30),
  },
];

const VOLUMES: Volume[] = [
  {
    volume_id: "b41e7c62-0000-4000-8000-00000000000a",
    state: "ready",
    content_sha256: "65bdee9bcd4eb58d909d7b38701bf1c65e1dc6e4af6733ae00e152b21997c25a",
    bytes: 48_226_304,
    voxels: 55_050_240,
    x_size: 512,
    y_size: 512,
    z_size: 210,
    jobs_run: 3,
    created_at: iso(70),
    expires_at: iso(-48),
  },
  {
    volume_id: "b41e7c62-0000-4000-8000-00000000000b",
    state: "ready",
    content_sha256: "f45e61c34c56af7b71711a6de54e8414dbc4cb52441003894f3370ed68d8feaa",
    bytes: 31_457_280,
    voxels: 20_971_520,
    x_size: 256,
    y_size: 256,
    z_size: 320,
    jobs_run: 1,
    created_at: iso(20),
    expires_at: iso(-100),
  },
];

/* -- the harness shell --------------------------------------------------- */

const SCENES = [
  "Overview",
  "Jobs — cards",
  "Jobs — table",
  "Job detail",
  "Keys",
  "Billing",
  "Checkout",
  "Empty + error states",
  "Primitives",
] as const;
type Scene = (typeof SCENES)[number];

const qc = new QueryClient({
  defaultOptions: { queries: { retry: false, staleTime: Infinity } },
});

function noop() {}

function OverviewScene() {
  const usage = makeUsage();
  return (
    <div className="flex flex-col gap-8">
      <SectionHeader label="This month" metric={`${ME.used_this_month} submitted · 5 days left`}>
        Quota counts submissions, not completions, and resets on the 1st (UTC).
      </SectionHeader>
      <div className="grid gap-4 lg:grid-cols-[1.4fr_1fr]">
        <Card title="Monthly quota">
          <QuotaMeter used={ME.used_this_month} limit={ME.monthly_quota} inFlight={3} />
        </Card>
        <Card title="Concurrency">
          <DonutRing
            value={ME.outstanding}
            max={ME.max_outstanding}
            label="1 on the GPU, 2 waiting"
            sub="Your cap is 6 outstanding jobs."
          />
        </Card>
      </div>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="Jobs / 28d" value={usage.total_jobs} hint={`${usage.total_prompts} structures requested`} />
        <StatCard label="GPU / 28d" value="1.0" unit="h" />
        <StatCard label="Queue depth" value={SYSTEM.queue_depth} tone="accent" hint="about 1m 36s to start" />
        <StatCard label="Worker" value="online" tone="ok" hint="1 running · snapshot 3s old" />
      </div>
      <Card title="Daily activity" eyebrow="Usage">
        <ColumnChart days={usage.days} />
      </Card>
      <Card title="Held series" eyebrow="Reusable uploads">
        <ul className="flex flex-col">
          {VOLUMES.map((v) => (
            <li key={v.volume_id} className="flex flex-wrap items-baseline gap-x-3 border-b border-border-soft py-2.5 last:border-0">
              <span className="font-mono text-xs text-ink tabular-nums">
                {v.x_size} × {v.y_size} × {v.z_size}
              </span>
              <span className="font-mono text-[11px] text-muted tabular-nums">{v.jobs_run} jobs run</span>
              <span className="ml-auto font-mono text-[11px] text-faint">expires in 48m</span>
            </li>
          ))}
        </ul>
      </Card>
      <Card title="Recent jobs">
        <div>
          {JOBS.slice(0, 5).map((j) => (
            <JobLine key={j.job_id} job={j} />
          ))}
        </div>
      </Card>
      <Card title="Unlimited-plan variant" eyebrow="Same meter, no ceiling">
        <QuotaMeter used={ME_UNLIMITED.used_this_month} limit={null} inFlight={2} />
      </Card>
      <Card title="Worker offline variant">
        <div className="grid gap-4 sm:grid-cols-2">
          <StatCard label="Worker" value="offline" tone="danger" hint="0 running · snapshot 3s old" />
          <StatCard label="Queue depth" value={SYSTEM_DOWN.queue_depth} tone="accent" hint="no worker to start it" />
        </div>
      </Card>
    </div>
  );
}

function JobsScene({ table }: { table: boolean }) {
  const { toasts, dismiss } = useToasts();
  return (
    <div>
      <SectionHeader label="Jobs" metric={`1–${JOBS.length} of ${JOBS.length}`}>
        Every segmentation submitted from Eclipse or the API.
      </SectionHeader>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Select aria-label="Filter by state" defaultValue="" className="max-w-48">
          <option value="">All states</option>
          {JOB_STATES.map((s) => (
            <option key={s} value={s}>
              {s.replace("_", " ")}
            </option>
          ))}
        </Select>
        <span className="font-mono text-[10px] text-faint">refreshing…</span>
      </div>
      {table ? (
        <Table head={["State", "Prompts", "Duration", "GPU", "Submitted", ""]}>
          {JOBS.map((j) => (
            <JobTableRow key={j.job_id} job={j} busy={false} onCancel={noop} onDownload={noop} />
          ))}
        </Table>
      ) : (
        <ul className="flex flex-col gap-3">
          {JOBS.map((j) => (
            <JobCard key={j.job_id} job={j} busy={false} onCancel={noop} onDownload={noop} />
          ))}
        </ul>
      )}
      <ToastStack toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}

function JobDetailScene() {
  // The real page fetches; here the same composition is rendered against a fixture
  // so the failed-job and expired-job layouts can be seen without a server.
  const [which, setWhich] = useState(0);
  const d = JOBS[which]!;
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap gap-2">
        {JOBS.map((j, i) => (
          <button
            key={j.job_id}
            onClick={() => setWhich(i)}
            className={`${buttonClass(i === which ? "primary" : "ghost", "sm")}`}
          >
            {j.state}
          </button>
        ))}
      </div>
      <SectionHeader label="Job" metric={`${d.prompts.length} prompts`}>
        Submitted {d.created_at}.
      </SectionHeader>
      <Card title="State">
        <div className="flex flex-wrap items-center gap-3">
          <StateBadge state={d.state} />
        </div>
        {(d.state === "queued" || d.state === "running") && (
          <div className="mt-4">
            <Progress value={d.progress} label={d.message ?? "Working"} />
          </div>
        )}
        {d.error && (
          <p className="mt-4 rounded-chip border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
            {d.error}
            <span className="ml-1 font-mono text-[11px] opacity-70">({d.failure_class})</span>
          </p>
        )}
      </Card>
      <Card title="Prompts" eyebrow={`${d.prompts.length} in this job`}>
        <div className="flex flex-wrap gap-1.5">
          {d.prompts.map((p, i) => (
            <PromptChip key={`${p}-${i}`} prompt={p} />
          ))}
        </div>
      </Card>
    </div>
  );
}

function KeysScene() {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <SectionHeader label="Workstation keys" metric={`2 active · ${KEYS.length} total`}>
        One key per workstation, with an expiry.
      </SectionHeader>
      <div className="flex flex-col gap-4">
        <Card title="Create a key">
          <div className="grid gap-3 sm:grid-cols-[1fr_12rem_auto] sm:items-end">
            <Field label="Label" hint="Name the workstation, so a revoked key is obvious later.">
              <Input placeholder="TPS-WS-04" />
            </Field>
            <Field label="Expires">
              <Select defaultValue="180">
                <option value="">Never</option>
                <option value="180">180 days</option>
              </Select>
            </Field>
            <Button variant="primary" onClick={() => setOpen(true)}>
              Create key
            </Button>
          </div>
        </Card>
        <Card title="Keys" padded={false}>
          <Table head={["Label", "Key", "Created", "Last used", "Status", ""]}>
            {KEYS.map((k) => (
              <Row key={k.id}>
                <Cell>{k.name}</Cell>
                <Cell mono>{k.prefix}…</Cell>
                <Cell mono>12d ago</Cell>
                <Cell mono>{k.last_used_at ? "9m ago" : "never"}</Cell>
                <Cell>{k.revoked_at ? "revoked" : k.expires_at ? "expires in 180d" : "no expiry"}</Cell>
                <Cell>
                  <div className="flex justify-end">
                    {!k.revoked_at && (
                      <Button size="sm" variant="danger">
                        Revoke
                      </Button>
                    )}
                  </div>
                </Cell>
              </Row>
            ))}
          </Table>
        </Card>
      </div>
      <Modal
        open={open}
        title="Copy this key now — it is not shown again"
        onClose={() => setOpen(false)}
        footer={
          <Button size="sm" variant="primary" onClick={() => setOpen(false)}>
            Done
          </Button>
        }
      >
        <p className="mb-3 text-sm text-ink-dim">Paste this into the plugin's Server &amp; API key panel.</p>
        <code className="block rounded-chip border border-border bg-ground p-3 font-mono text-xs break-all text-accent-3 select-all">
          vxt_9fA2kQ7xR3mNpL8sT2vW6yB4zC1dE5gH0jK9nM3qS7uX
        </code>
      </Modal>
    </div>
  );
}

function BillingScene() {
  return (
    <div className="flex flex-col gap-6">
      <SectionHeader label="Billing" metric="5 days to the reset">
        Your quota and what each plan includes.
      </SectionHeader>
      <Alert tone="info">Self-serve billing is not connected yet.</Alert>
      <div className="grid gap-4 lg:grid-cols-[1.4fr_1fr]">
        <Card title="Current allowance">
          <QuotaMeter used={ME.used_this_month} limit={ME.monthly_quota} inFlight={3} />
        </Card>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-1">
          <StatCard label="Concurrent cap" value={6} unit="jobs" hint="Queued plus running at once." />
          <StatCard label="Used this month" value={ME.used_this_month} hint="Submissions, from the 1st (UTC)." />
        </div>
      </div>
      <div className="grid gap-4 lg:grid-cols-3">
        {PLANS.map((p) => (
          <PlanCard
            key={p.id}
            plan={p}
            current={p.id === "clinician"}
            cta={<span className={buttonClass(p.featured ? "primary" : "ghost", "md")}>Choose {p.name}</span>}
          />
        ))}
      </div>
    </div>
  );
}

function CheckoutScene() {
  return (
    <div className="flex flex-col gap-6">
      <SectionHeader label="Checkout" metric="billing not connected">
        You selected Pro. Nothing has been charged.
      </SectionHeader>
      <div className="grid gap-4 lg:grid-cols-[1fr_1.3fr] lg:items-start">
        <PlanCard plan={PLANS[1]!} />
        <Card title="Pro — noted">
          <p className="text-sm text-ink-dim">Self-serve payment is not switched on yet.</p>
          <div className="mt-5 flex flex-wrap gap-2">
            <span className={buttonClass("primary", "md")}>Email us to start</span>
            <span className={buttonClass("ghost", "md")}>Back to billing</span>
          </div>
        </Card>
      </div>
    </div>
  );
}

function StatesScene() {
  return (
    <div className="flex flex-col gap-6">
      <SectionHeader label="States" metric="the ones nobody looks at">
        Loading, empty and error for each panel — the three that ship broken because
        the happy path is the only one anybody opens.
      </SectionHeader>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Loading">
          <div className="flex flex-col gap-3">
            <Skeleton className="h-7 w-40" />
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-32 w-full" />
          </div>
        </Card>
        <Card title="Error">
          <Alert>Could not load recent jobs: 503 Service Unavailable</Alert>
          <div className="mt-3">
            <Alert tone="info">Self-serve billing is not connected yet.</Alert>
          </div>
          <div className="mt-3">
            <Alert tone="ok">Cancellation requested.</Alert>
          </div>
        </Card>
        <Card title="Empty">
          <Empty action={<span className={buttonClass("primary", "sm")}>Create a workstation key</span>}>
            No jobs in the last 28 days. Segmentations submitted from Eclipse appear here.
          </Empty>
        </Card>
        <Card title="Spinners">
          <div className="flex flex-col items-start gap-3">
            <Spinner label="Signing in…" />
            <Spinner label="Loading your account…" />
            <Spinner label="Signing out…" />
          </div>
        </Card>
        <Card title="Unreadable allowance">
          <p className="text-sm text-muted">
            Your allowance could not be read. The figures below are the published plans, not
            your account.
          </p>
        </Card>
        <Card title="Concurrency unavailable">
          <p className="text-sm text-muted">Your concurrency cap could not be read.</p>
        </Card>
      </div>
    </div>
  );
}

function PrimitivesScene() {
  const { toasts, push, dismiss } = useToasts();
  return (
    <div className="flex flex-col gap-6">
      <SectionHeader label="Primitives" metric="every variant">
        If a control is not here it is not in the library, and a page is hand-rolling it.
      </SectionHeader>
      <Card title="Buttons">
        <div className="flex flex-wrap items-center gap-2">
          {(["primary", "ghost", "danger", "subtle"] as const).map((v) =>
            (["sm", "md", "lg"] as const).map((s) => (
              <Button key={`${v}-${s}`} variant={v} size={s}>
                {v}/{s}
              </Button>
            )),
          )}
          <Button disabled>disabled</Button>
        </div>
      </Card>
      <Card title="Job states">
        <div className="flex flex-wrap gap-2">
          {JOB_STATES.map((s) => (
            <StateBadge key={s} state={s} />
          ))}
        </div>
      </Card>
      <Card title="Prompt chips — all 23 structure colours">
        {/* The check that matters: Tailwind v4 prunes any @theme colour no utility
            references, and these are chosen at runtime. If 22 of these render with no
            swatch, the tokens were pruned again. */}
        <div className="flex flex-wrap gap-1.5">
          {[
            "liver", "spleen", "stomach", "pancreas", "aorta", "inferior vena cava",
            "portal vein", "spinal cord", "rib", "t12 vertebra", "colon",
            "erector spinae", "costal cartilage", "adrenal gland", "left kidney",
            "heart", "lung", "bladder", "oesophagus", "rectum", "brain",
            "parotid gland", "ptv",
          ].map((p) => (
            <PromptChip key={p} prompt={p} />
          ))}
        </div>
      </Card>
      <Card title="Progress">
        <div className="flex flex-col gap-4">
          {[0, 0.01, 0.42, 0.99, 1].map((v) => (
            <Progress key={v} value={v} label={`${Math.round(v * 100)}%`} />
          ))}
        </div>
      </Card>
      <Card title="Toasts">
        <div className="flex gap-2">
          <Button onClick={() => push("Cancellation requested.")}>push ok</Button>
          <Button variant="danger" onClick={() => push("403 Forbidden", "danger")}>
            push error
          </Button>
        </div>
      </Card>
      <ToastStack toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}

function Harness() {
  const [scene, setScene] = useState<Scene>("Overview");
  return (
    <AppShell email={ME.email} onSignOut={noop}>
      <div className="mb-6 flex flex-wrap items-center gap-1.5 rounded-card border border-border bg-surface p-2">
        <span className="mr-2 font-mono text-[10px] tracking-label uppercase text-faint">
          preview
        </span>
        {SCENES.map((s) => (
          <button
            key={s}
            onClick={() => setScene(s)}
            className={
              "rounded-chip px-2.5 py-1 font-mono text-[11px] transition-colors " +
              (scene === s ? "bg-surface-2 text-accent" : "text-muted hover:text-ink")
            }
          >
            {s}
          </button>
        ))}
      </div>

      {scene === "Overview" && <OverviewScene />}
      {scene === "Jobs — cards" && <JobsScene table={false} />}
      {scene === "Jobs — table" && <JobsScene table />}
      {scene === "Job detail" && <JobDetailScene />}
      {scene === "Keys" && <KeysScene />}
      {scene === "Billing" && <BillingScene />}
      {scene === "Checkout" && <CheckoutScene />}
      {scene === "Empty + error states" && <StatesScene />}
      {scene === "Primitives" && <PrimitivesScene />}
    </AppShell>
  );
}

const el = document.getElementById("root");
if (!el) throw new Error("Root element #root not found");

createRoot(el).render(
  <StrictMode>
    <QueryClientProvider client={qc}>
      {/* MemoryRouter, not BrowserRouter: the harness has no server behind it, so a
          real history push to /jobs/<id> would 404 on reload. Links still render and
          NavLink still resolves its active state. */}
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route path="*" element={<Harness />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  </StrictMode>,
);
