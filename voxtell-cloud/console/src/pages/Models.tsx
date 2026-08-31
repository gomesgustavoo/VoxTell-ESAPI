// Models — what this deployment can be asked to segment, and under which licence.
//
// Why the console shows this at all: the catalog is the contract between the Eclipse
// plugin and the server. When a planner says "I can't find the rectum in the list",
// this page is where you check whether the structure exists, which model produces it,
// and what a clinic would have to type for auto-detect to match it.
//
// The licence column is the reason this is a page and not a tooltip. CADS publishes
// three weight variants under three different licences, selected by a CLI flag, and
// only one of them permits commercial use. "Which licence produced this patient's
// contours" is a question a clinic may have to answer later, so it is shown here and
// recorded per job rather than inferred from whatever is deployed today.

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api, type Catalog, type CatalogModel } from "../lib/api";
import {
  Alert,
  Card,
  Empty,
  Input,
  SectionHeader,
  Skeleton,
  StatCard,
} from "../components/ui";

/** Licences that permit commercial use. Anything else is called out. */
const COMMERCIAL_OK = new Set(["CC-BY-SA-4.0", "Apache-2.0", "MIT"]);

function LicenceBadge({ licence }: { licence: string }) {
  const ok = COMMERCIAL_OK.has(licence);
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 font-mono text-[10px] whitespace-nowrap ${
        ok
          ? "border-border text-muted"
          : "border-danger/40 text-danger"
      }`}
      title={
        ok
          ? "Permits commercial use"
          : "Non-commercial weights — not usable in a paid deployment"
      }
    >
      {licence}
    </span>
  );
}

function ModelRow({ model, structures }: { model: CatalogModel; structures: number }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-border py-2.5 last:border-b-0">
      <span className="font-mono text-xs text-faint">{model.key}</span>
      <span className="min-w-0 flex-1 truncate text-sm text-ink">{model.display_name}</span>
      <span className="text-xs text-muted">{model.region}</span>
      <span className="font-mono text-xs text-faint tabular-nums">
        {model.kind === "prompt" ? "free text" : `${structures} structures`}
      </span>
      <LicenceBadge licence={model.weights_licence} />
    </div>
  );
}

export default function Models() {
  const [filter, setFilter] = useState("");

  // No token: the catalog is unauthenticated, and staleTime is generous because it
  // only changes on a deployment.
  const { data, isLoading, error } = useQuery<Catalog>({
    queryKey: ["catalog"],
    queryFn: () => api.catalog(),
    staleTime: 10 * 60 * 1000,
  });

  const perModel = useMemo(() => {
    const counts = new Map<string, number>();
    for (const s of data?.structures ?? []) {
      counts.set(s.source_model, (counts.get(s.source_model) ?? 0) + 1);
    }
    return counts;
  }, [data]);

  const groups = useMemo(() => {
    if (!data) return [];
    const q = filter.trim().toLowerCase();
    const matches = data.structures.filter((s) => {
      if (!q) return true;
      return (
        s.display_name.toLowerCase().includes(q) ||
        s.id.toLowerCase().includes(q) ||
        s.aliases.some((a) => a.includes(q.replace(/[^a-z0-9]/g, "")))
      );
    });

    const byGroup = new Map<string, typeof matches>();
    for (const s of matches) {
      const list = byGroup.get(s.group) ?? [];
      list.push(s);
      byGroup.set(s.group, list);
    }

    // The server's order, then anything it did not order — never dropped, because a
    // structure that exists must always be findable here.
    const ordered = data.group_order.filter((g) => byGroup.has(g));
    const extra = [...byGroup.keys()].filter((g) => !data.group_order.includes(g)).sort();
    return [...ordered, ...extra].map((g) => ({ group: g, items: byGroup.get(g)! }));
  }, [data, filter]);

  if (isLoading) {
    return (
      <div className="flex flex-col gap-4">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (error || !data) {
    return (
      <Alert>
        The model catalog could not be loaded
        {error instanceof Error ? `: ${error.message}` : ""}.
      </Alert>
    );
  }

  const nonCommercial = data.models.filter((m) => !COMMERCIAL_OK.has(m.weights_licence));
  const matched = groups.reduce((sum, g) => sum + g.items.length, 0);

  return (
    <div className="flex flex-col gap-8">
      <div>
        <SectionHeader label="Catalog" metric={`v${data.version}`}>
          Every model and structure this deployment can be asked for. The Eclipse plugin
          builds its picker from exactly this document, so what is here is what a planner
          sees.
        </SectionHeader>

        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatCard label="Models" value={data.models.length} />
          <StatCard label="Structures" value={data.structures.length} />
          <StatCard label="Presets" value={data.presets.length} />
          <StatCard
            label="Non-commercial"
            value={nonCommercial.length}
            tone={nonCommercial.length > 0 ? "danger" : "ok"}
            hint={
              nonCommercial.length > 0
                ? "Weights that may not be used in a paid deployment"
                : "All deployed weights permit commercial use"
            }
          />
        </div>
      </div>

      <Card title="Models" eyebrow="What runs">
        <div className="flex flex-col">
          {data.models.map((m) => (
            <ModelRow key={m.key} model={m} structures={perModel.get(m.key) ?? 0} />
          ))}
        </div>
      </Card>

      {data.presets.length > 0 && (
        <Card
          title="Presets"
          eyebrow="Named selections"
          action={
            <span className="font-mono text-xs text-faint">
              picked instead of ticking {data.structures.length} boxes
            </span>
          }
        >
          <div className="flex flex-col gap-2.5">
            {data.presets.map((p) => (
              <div key={p.key} className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="text-sm text-ink">{p.display_name}</span>
                <span className="font-mono text-xs text-faint tabular-nums">
                  {p.structure_ids.length} structures
                </span>
                <span className="font-mono text-xs text-faint">
                  {p.models.join(" + ")}
                </span>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Card
        title="Structures"
        eyebrow="What can be asked for"
        action={
          <div className="flex items-center gap-3">
            <span className="font-mono text-xs text-faint tabular-nums">
              {matched} of {data.structures.length}
            </span>
            <Input
              value={filter}
              onChange={(e) => setFilter(e.currentTarget.value)}
              placeholder="Search names and aliases…"
              className="w-56"
              aria-label="Search structures"
            />
          </div>
        }
      >
        {matched === 0 ? (
          <Empty>
            Nothing matches “{filter}”. Aliases ignore case and punctuation, so
            <span className="font-mono"> Kidney_R</span> and
            <span className="font-mono"> kidney r</span> both find the same structure.
          </Empty>
        ) : (
          <div className="flex flex-col gap-6">
            {groups.map(({ group, items }) => (
              <div key={group}>
                <p className="mb-2 font-mono text-[10px] font-semibold tracking-label uppercase text-faint">
                  {group}
                </p>
                <div className="grid gap-x-6 gap-y-1 sm:grid-cols-2">
                  {items.map((s) => (
                    <div key={s.id} className="flex items-baseline gap-2">
                      <span className="min-w-0 flex-1 truncate text-sm text-ink" title={s.id}>
                        {s.display_name}
                      </span>
                      <span className="font-mono text-[10px] whitespace-nowrap text-faint">
                        {s.source_model}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
