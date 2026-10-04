"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import {
  getAgentActivity,
  getBackends,
  getMemoryHealth,
  getRetrievalQuality,
  getServiceHealth,
  listOrganizations,
} from "@/lib/api";
import type {
  AgentActivity,
  Backends,
  MemoryHealth,
  Organization,
  RetrievalQuality,
  ServiceHealth,
} from "@/lib/types";
import { AgentsView, GraphView, MemoryView, RetrievalView, ServiceView } from "./OpsViews";

const VIEWS = [
  { id: "agents", label: "Agents" },
  { id: "memory", label: "Memory" },
  { id: "retrieval", label: "Retrieval" },
  { id: "graph", label: "Graph" },
  { id: "service", label: "Service" },
] as const;
type ViewId = (typeof VIEWS)[number]["id"];
const WINDOWS = [
  { hours: 1, label: "Last hour" },
  { hours: 24, label: "Last 24 hours" },
  { hours: 168, label: "Last 7 days" },
];
const REFRESH_MS = 30_000;

type Loaded =
  | { key: string; view: "agents"; data: AgentActivity }
  | { key: string; view: "memory"; data: MemoryHealth }
  | { key: string; view: "retrieval"; data: RetrievalQuality }
  | { key: string; view: "graph"; data: Backends }
  | { key: string; view: "service"; data: ServiceHealth };

export function OpsPanel() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const requested = params.get("view");
  const view: ViewId = VIEWS.some((v) => v.id === requested) ? (requested as ViewId) : "agents";
  const hours = Number(params.get("hours")) || 24;
  const tenantParam = params.get("tenant") ?? "";
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [failure, setFailure] = useState<{ key: string; message: string } | null>(null);
  const [tick, setTick] = useState(0);

  const update = (changes: Record<string, string>) => {
    const next = new URLSearchParams(params.toString());
    for (const [name, value] of Object.entries(changes)) {
      if (value) next.set(name, value);
      else next.delete(name);
    }
    const query = next.toString();
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
  };

  useEffect(() => {
    listOrganizations()
      .then(({ items }) => setOrganizations(items))
      .catch(() => setOrganizations([]));
    const timer = setInterval(() => setTick((n) => n + 1), REFRESH_MS);
    return () => clearInterval(timer);
  }, []);

  // Memory health is always about one tenant; the others can span all of them.
  const fullest = [...organizations].sort((a, b) => b.memories - a.memories)[0]?.id ?? "";
  const tenant = view === "memory" ? tenantParam || fullest : tenantParam;
  const key = `${view}|${tenant}|${hours}`;

  useEffect(() => {
    if (view === "memory" && !tenant) return;
    let cancelled = false;
    const organization_id = tenant || undefined;
    const request: Promise<Loaded> =
      view === "agents"
        ? getAgentActivity({ organization_id, hours }).then((data) => ({ key, view, data }))
        : view === "memory"
          ? getMemoryHealth({ organization_id: tenant }).then((data) => ({ key, view, data }))
          : view === "retrieval"
            ? getRetrievalQuality({ organization_id, hours }).then((data) => ({ key, view, data }))
            : view === "graph"
              ? getBackends({ hours }).then((data) => ({ key, view, data }))
              : getServiceHealth({ hours }).then((data) => ({ key, view, data }));
    request
      .then((result) => {
        if (!cancelled) setLoaded(result);
      })
      .catch((e: Error) => {
        if (!cancelled) setFailure({ key, message: e.message });
      });
    return () => {
      cancelled = true;
    };
  }, [view, tenant, hours, key, tick]);

  const current = loaded?.key === key ? loaded : null;
  const error = failure?.key === key && !current ? failure.message : null;

  return (
    <div className="ops">
      <div className="toolbar">
        <div className="tabs" role="tablist" aria-label="Operations views">
          {VIEWS.map((v) => (
            <button
              key={v.id}
              type="button"
              role="tab"
              aria-selected={v.id === view}
              onClick={() => update({ view: v.id === "agents" ? "" : v.id })}
            >
              {v.label}
            </button>
          ))}
        </div>
        <span className="spacer" />
        {view === "service" || view === "graph" ? (
          <span className="muted">All tenants</span>
        ) : (
          <select
            aria-label="Tenant"
            value={tenant}
            onChange={(e) => update({ tenant: e.target.value })}
          >
            {view === "memory" ? null : <option value="">All tenants</option>}
            {organizations.map((o) => (
              <option key={o.id} value={o.id}>
                {o.name}
              </option>
            ))}
          </select>
        )}
        {view === "memory" ? (
          <span className="muted">Current state</span>
        ) : (
          <select
            aria-label="Window"
            value={hours}
            onChange={(e) => update({ hours: e.target.value === "24" ? "" : e.target.value })}
          >
            {WINDOWS.map((w) => (
              <option key={w.hours} value={w.hours}>
                {w.label}
              </option>
            ))}
          </select>
        )}
      </div>
      {error ? <div className="error-box">This view could not be loaded: {error}</div> : null}
      {!current && !error ? <p className="muted">Loading…</p> : null}
      {current?.view === "agents" ? <AgentsView data={current.data} /> : null}
      {current?.view === "memory" ? <MemoryView data={current.data} /> : null}
      {current?.view === "retrieval" ? <RetrievalView data={current.data} /> : null}
      {current?.view === "graph" ? <GraphView data={current.data} /> : null}
      {current?.view === "service" ? <ServiceView data={current.data} /> : null}
    </div>
  );
}
