"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { getMap, listOrganizations, listWorkspaces } from "@/lib/api";
import {
  NO_FILTERS,
  applyFilters,
  clusterColor,
  colorScale,
  highlightFromTrace,
  type ColorBy,
  type Filters,
  type Pulse,
} from "@/lib/map";
import type { MapSnapshot, Organization, TraceDetail, Workspace } from "@/lib/types";
import { LinkToggles, MapToolbar } from "./MapToolbar";
import { MapView } from "./MapView";

const EMPTY: MapSnapshot = { nodes: [], links: [], clusters: [], truncated: false };
const NO_PULSES: Pulse[] = [];
const NO_HITS = new Map<string, number>();

export function MapPanel({ trace }: { trace?: TraceDetail }) {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [organization, setOrganization] = useState<string>(trace?.organization_id ?? "");
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspace, setWorkspace] = useState("");
  const [snapshot, setSnapshot] = useState<MapSnapshot>(EMPTY);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [colorBy, setColorBy] = useState<ColorBy>("type");
  const [localSelected, setLocalSelected] = useState<string | null>(null);
  const [focus, setFocus] = useState<{ node?: string; cluster?: number; seq: number } | null>(null);

  // On the map page the selection lives in the address, so a memory can be linked to.
  const selected = trace ? localSelected : params.get("memory");
  const select = useCallback(
    (id: string | null) => {
      if (trace) setLocalSelected(id);
      else router.replace(id ? `${pathname}?memory=${id}` : pathname, { scroll: false });
    },
    [trace, router, pathname],
  );

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

  useEffect(() => {
    if (!organization) return;
    let cancelled = false;
    listWorkspaces(organization)
      .then(({ items }) => {
        if (!cancelled) setWorkspaces(items);
      })
      .catch(() => {
        if (!cancelled) setWorkspaces([]);
      });
    return () => {
      cancelled = true;
    };
  }, [organization]);

  useEffect(() => {
    if (!organization) return;
    let cancelled = false;
    getMap({ organization_id: organization, workspace_id: workspace || undefined })
      .then((data) => {
        if (cancelled) return;
        setSnapshot(data);
        setLoaded(true);
        setError(null);
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [organization, workspace]);

  const visible = useMemo(() => applyFilters(snapshot, filters), [snapshot, filters]);
  const scale = useMemo(() => colorScale(visible.nodes, colorBy), [visible.nodes, colorBy]);
  const highlight = useMemo(() => (trace ? highlightFromTrace(trace) : null), [trace]);
  const current = organizations.find((o) => o.id === organization);
  const isEmpty = loaded && snapshot.nodes.length === 0;
  const filteredOut = loaded && !isEmpty && visible.nodes.length === 0;

  const changeOrganization = (id: string) => {
    setOrganization(id);
    setWorkspace("");
    setWorkspaces([]);
    setFilters(NO_FILTERS);
    setLoaded(false);
    setSnapshot(EMPTY);
    select(null);
  };
  const changeWorkspace = (id: string) => {
    setWorkspace(id);
    setFilters(NO_FILTERS);
    setLoaded(false);
  };

  return (
    <div className={`map${trace ? " compact" : ""}`}>
      <MapToolbar
        organizations={organizations}
        organization={organization}
        onOrganization={changeOrganization}
        workspaces={workspaces}
        workspace={workspace}
        onWorkspace={changeWorkspace}
        nodes={snapshot.nodes}
        filters={filters}
        onFilters={setFilters}
        colorBy={colorBy}
        onColorBy={setColorBy}
        compact={!!trace}
      />
      <div className="map-meta">
        {highlight ? (
          <div className="legend" data-testid="highlight-legend">
            <span>
              <i className="swatch lexical" /> lexical {highlight.lexical.size}
            </span>
            <span>
              <i className="swatch vector" /> vector {highlight.vector.size}
            </span>
            <span>
              <i className="swatch hit" /> final hits{" "}
              <b data-testid="highlight-hit-count">{highlight.hits.size}</b>
            </span>
          </div>
        ) : (
          <>
            <div className="legend" data-testid="map-legend" aria-label={`Colors by ${colorBy}`}>
              {scale.legend.map((entry) => (
                <span key={entry.label}>
                  <i className="swatch hex" style={{ background: entry.color }} /> {entry.label}
                </span>
              ))}
              <span className="muted">size = times recalled</span>
            </div>
            <LinkToggles filters={filters} onFilters={setFilters} />
          </>
        )}
        <span className="spacer" />
        <span className="stats mono">
          <span data-testid="map-node-count">{visible.nodes.length}</span> memories,{" "}
          <span data-testid="map-link-count">{visible.links.length}</span> links,{" "}
          <span data-testid="map-cluster-count">{visible.clusters.length}</span> clusters
          {snapshot.truncated ? (
            <span className="degraded" title="Only the newest memories are drawn">
              {" "}
              (newest {snapshot.nodes.length} shown)
            </span>
          ) : null}
        </span>
      </div>
      {visible.clusters.length > 0 && !trace ? (
        <div className="cluster-list" data-testid="map-cluster-list" aria-label="Clusters">
          {visible.clusters.map((cluster) => (
            <button
              key={cluster.id}
              type="button"
              onClick={() => setFocus({ cluster: cluster.id, seq: Date.now() })}
              title={`Zoom to ${cluster.label}`}
            >
              <i className="swatch" style={{ background: clusterColor(cluster.id) }} />
              {cluster.label}
              <span className="mono muted">{cluster.size}</span>
            </button>
          ))}
        </div>
      ) : null}
      {error ? <div className="error-box">The map could not be loaded: {error}</div> : null}
      <div className="map-frame">
        {isEmpty ? (
          <div className="graph-empty" data-testid="map-empty">
            <h2>No memories in {current?.name ?? "this tenant"} yet</h2>
            <p>
              Memories appear here as agents store them with <code>memory_remember</code> or{" "}
              <code>memory_ingest</code>. Ones about the same thing link up and gather into named
              clusters on their own.
            </p>
            <p className="muted">
              The Live screen shows every request as it happens, including ones that stored
              nothing.
            </p>
          </div>
        ) : filteredOut ? (
          <div className="graph-empty" data-testid="map-filtered-out">
            <h2>No memories match these filters</h2>
            <p>
              <button type="button" className="link" onClick={() => setFilters(NO_FILTERS)}>
                Clear the filters
              </button>{" "}
              to see all {snapshot.nodes.length}.
            </p>
          </div>
        ) : (
          <MapView
            snapshot={visible}
            colorBy={colorBy}
            selectedId={selected}
            focus={focus}
            highlight={highlight}
            pulses={NO_PULSES}
            searchHits={NO_HITS}
            onSelect={select}
          />
        )}
      </div>
    </div>
  );
}
