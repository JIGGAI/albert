"use client";

import { useEffect, useMemo, useState } from "react";
import { getGraph, listOrganizations } from "@/lib/api";
import { EMPTY_HIGHLIGHT, highlightCount, highlightFromTrace } from "@/lib/highlight";
import type { GraphNode, GraphSnapshot, Organization, TraceDetail } from "@/lib/types";
import { DetailPanel } from "./DetailPanel";
import { Graph3D } from "./Graph3D";
import { TimeSlider } from "./TimeSlider";

const EMPTY: GraphSnapshot = { nodes: [], edges: [], truncated: false };

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

export function ExplorerPanel({ trace }: { trace?: TraceDetail }) {
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [organization, setOrganization] = useState<string>(trace?.organization_id ?? "");
  const [snapshot, setSnapshot] = useState<GraphSnapshot>(EMPTY);
  const [loaded, setLoaded] = useState(false);
  const [asOf, setAsOf] = useState<number | null>(null);
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listOrganizations()
      .then(({ items }) => {
        setOrganizations(items);
        // Default to the tenant with the most in it, so the first view is never
        // an empty one when something exists to look at.
        const fullest = [...items].sort((a, b) => b.memories - a.memories)[0];
        setOrganization((current) => current || fullest?.id || "");
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  const [range, setRange] = useState<{ min: number; max: number } | null>(null);
  useEffect(() => {
    if (!organization) return;
    let cancelled = false;
    const params: Parameters<typeof getGraph>[0] = { organization_id: organization };
    if (asOf !== null) params.temporal_as_of = new Date(asOf).toISOString();
    getGraph(params)
      .then((data) => {
        if (cancelled) return;
        setSnapshot(data);
        setLoaded(true);
        setError(null);
        if (asOf === null) {
          const starts = data.edges.map((e) => new Date(e.valid_from).getTime());
          const min = starts.length ? Math.min(...starts) - 1000 : Date.now() - 60_000;
          setRange({ min, max: Date.now() });
        }
      })
      .catch((e: Error) => setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [organization, asOf]);

  const highlight = useMemo(() => (trace ? highlightFromTrace(trace) : null), [trace]);
  const hits = highlightCount(highlight ?? EMPTY_HIGHLIGHT);
  const current = organizations.find((o) => o.id === organization);
  const isEmpty = loaded && snapshot.nodes.length === 0;
  const rewound = asOf !== null && range !== null && asOf < range.max - 1000;

  return (
    <div className="explorer">
      <div className="toolbar">
        <select
          aria-label="Tenant"
          value={organization}
          onChange={(e) => {
            setOrganization(e.target.value);
            setAsOf(null);
            setLoaded(false);
            setSelected(null);
          }}
        >
          {organization && !current ? <option value={organization}>selected tenant</option> : null}
          {organizations.map((o) => (
            <option key={o.id} value={o.id}>
              {`${o.name} · ${plural(o.memories, "memory", "memories")}`}
            </option>
          ))}
          {organizations.length === 0 && !organization ? <option value="">no tenants yet</option> : null}
        </select>
        {range && !isEmpty ? (
          <TimeSlider min={range.min} max={range.max} value={asOf ?? range.max} onChange={setAsOf} />
        ) : null}
        <span className="spacer" />
        <span className="stats mono">
          <span data-testid="graph-node-count">{snapshot.nodes.length}</span> nodes,{" "}
          <span data-testid="graph-edge-count">{snapshot.edges.length}</span> edges
          {snapshot.truncated ? <span className="degraded"> (capped)</span> : null}
        </span>
      </div>
      {highlight ? (
        <div className="legend" data-testid="highlight-legend">
          <span>
            <i className="swatch lexical" /> lexical {highlight.lexical.size}
          </span>
          <span>
            <i className="swatch vector" /> vector {highlight.vector.size}
          </span>
          <span>
            <i className="swatch graph" /> graph {highlight.graph.size}
          </span>
          <span>
            <i className="swatch hit" /> final hits <b data-testid="highlight-hit-count">{hits}</b>
          </span>
        </div>
      ) : (
        <div className="legend" aria-label="Node colors">
          <span>
            <i className="swatch entity" /> entity
          </span>
          <span>
            <i className="swatch internal" /> memory, internal
          </span>
          <span>
            <i className="swatch confidential" /> confidential
          </span>
          <span>
            <i className="swatch restricted" /> restricted
          </span>
          {current ? (
            <span>
              {plural(current.entities, "entity", "entities")},{" "}
              {plural(current.relationships, "live edge", "live edges")} in this tenant
            </span>
          ) : null}
        </div>
      )}
      {error ? <div className="error-box">Graph could not be loaded: {error}</div> : null}
      <div className="graph-frame">
        {isEmpty ? (
          <div className="graph-empty" data-testid="graph-empty">
            {rewound && current && current.memories > 0 ? (
              <>
                <h2>Nothing existed at this point in time</h2>
                <p>Move the slider right to see {current.name} as it is now.</p>
              </>
            ) : (
              <>
                <h2>No memories in {current?.name ?? "this tenant"} yet</h2>
                <p>
                  Memories appear here as agents store them with <code>memory_remember</code> or{" "}
                  <code>memory_ingest</code>. Entities and the edges between them appear once a
                  memory states a relationship, such as one thing using or owning another.
                </p>
                <p className="muted">
                  The Live screen shows every request as it happens, including ones that stored
                  nothing.
                </p>
              </>
            )}
          </div>
        ) : (
          <Graph3D snapshot={snapshot} highlight={highlight} onSelect={setSelected} />
        )}
        <DetailPanel node={selected} onClose={() => setSelected(null)} />
      </div>
    </div>
  );
}
