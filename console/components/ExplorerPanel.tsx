"use client";

import { useEffect, useMemo, useState } from "react";
import { getGraph, listTraces, shortId } from "@/lib/api";
import { EMPTY_HIGHLIGHT, highlightCount, highlightFromTrace } from "@/lib/highlight";
import type { GraphNode, GraphSnapshot, TraceDetail } from "@/lib/types";
import { DetailPanel } from "./DetailPanel";
import { Graph3D } from "./Graph3D";
import { TimeSlider } from "./TimeSlider";

const EMPTY: GraphSnapshot = { nodes: [], edges: [], truncated: false };

export function ExplorerPanel({ trace }: { trace?: TraceDetail }) {
  const [organizations, setOrganizations] = useState<string[]>([]);
  const [organization, setOrganization] = useState<string>(trace?.organization_id ?? "");
  const [snapshot, setSnapshot] = useState<GraphSnapshot>(EMPTY);
  const [asOf, setAsOf] = useState<number | null>(null);
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Tenants are discovered from recent traces; the console has no tenant list of its own.
  useEffect(() => {
    listTraces({ limit: 200 })
      .then((page) => {
        const ids = Array.from(
          new Set(page.items.map((t) => t.organization_id).filter((id): id is string => !!id)),
        );
        setOrganizations(ids);
        setOrganization((current) => current || ids[0] || "");
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

  return (
    <div className="explorer">
      <div className="toolbar">
        <select
          aria-label="Tenant"
          value={organization}
          onChange={(e) => {
            setOrganization(e.target.value);
            setAsOf(null);
          }}
        >
          {organization && !organizations.includes(organization) ? (
            <option value={organization}>{shortId(organization)}</option>
          ) : null}
          {organizations.map((id) => (
            <option key={id} value={id}>
              tenant {shortId(id)}
            </option>
          ))}
          {organizations.length === 0 && !organization ? <option value="">no tenants yet</option> : null}
        </select>
        {range ? (
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
      ) : null}
      {error ? <div className="error-box">Graph could not be loaded: {error}</div> : null}
      <div className="graph-frame">
        <Graph3D snapshot={snapshot} highlight={highlight} onSelect={setSelected} />
        <DetailPanel node={selected} onClose={() => setSelected(null)} />
      </div>
    </div>
  );
}
