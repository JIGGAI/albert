"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getMap, listOrganizations, listWorkspaces, openTraceStream } from "@/lib/api";
import {
  NO_FILTERS,
  PULSE_MS,
  applyFilters,
  clusterColor,
  colorScale,
  eventFromTrace,
  highlightFromTrace,
  type ColorBy,
  type Filters,
  type MapEvent,
  type Pulse,
} from "@/lib/map";
import type {
  MapSearchHit,
  MapSnapshot,
  MemoryDetail,
  Organization,
  TraceDetail,
  Workspace,
} from "@/lib/types";
import { MapSearch } from "./MapSearch";
import { LinkToggles, MapToolbar } from "./MapToolbar";
import { MapView } from "./MapView";
import { ReadingPane } from "./ReadingPane";
import { ReplayBar, type LiveState, type MapMode } from "./ReplayBar";

const EMPTY: MapSnapshot = { nodes: [], links: [], clusters: [], truncated: false };
const NO_RECALLS = new Map<string, number>();
const TICKER_LENGTH = 4;
const REFETCH_DELAY_MS = 1200;

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
  const [mode, setMode] = useState<MapMode>("live");
  const [liveState, setLiveState] = useState<LiveState>("connecting");
  const [pulses, setPulses] = useState<Pulse[]>([]);
  const [pulseCount, setPulseCount] = useState(0);
  const [ticker, setTicker] = useState<MapEvent[]>([]);
  const [liveRecalls, setLiveRecalls] = useState<{ on: MapSnapshot; counts: Map<string, number> }>({
    on: EMPTY,
    counts: NO_RECALLS,
  });
  const [reload, setReload] = useState(0);
  const [hits, setHits] = useState<MapSearchHit[] | null>(null);
  const refetch = useRef<ReturnType<typeof setTimeout> | null>(null);

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
        setLiveRecalls({ on: data, counts: new Map() });
        setLoaded(true);
        setError(null);
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [organization, workspace, reload]);

  /** Show one recall or store on the map: pulse its memories and note it in the ticker. */
  const show = useCallback((event: MapEvent, live: boolean) => {
    const at = performance.now();
    setPulses((current) => [
      ...current.filter((pulse) => at - pulse.at < PULSE_MS),
      ...event.ids.map((nodeId) => ({
        key: `${event.traceId}-${nodeId}-${at}`,
        nodeId,
        kind: event.kind,
        at,
      })),
    ]);
    setPulseCount((count) => count + 1);
    setTicker((current) =>
      [event, ...current.filter((e) => e.traceId !== event.traceId)].slice(0, TICKER_LENGTH),
    );
    if (!live) return;
    if (event.kind === "recall") {
      setLiveRecalls((current) => {
        const counts = new Map(current.counts);
        for (const id of event.ids) counts.set(id, (counts.get(id) ?? 0) + 1);
        return { on: current.on, counts };
      });
    }
    // Something the map does not have yet, or freshly indexed links: fetch again,
    // once, after the burst settles.
    if (event.kind === "store") {
      if (refetch.current) clearTimeout(refetch.current);
      refetch.current = setTimeout(() => setReload((n) => n + 1), REFETCH_DELAY_MS);
    }
  }, []);

  useEffect(() => {
    if (pulses.length === 0) return;
    const timer = setTimeout(() => {
      const now = performance.now();
      setPulses((current) => current.filter((pulse) => now - pulse.at < PULSE_MS));
    }, PULSE_MS + 120);
    return () => clearTimeout(timer);
  }, [pulses]);

  useEffect(() => {
    if (trace || mode !== "live" || !organization) return;
    const close = openTraceStream(
      (summary) => {
        if (summary.organization_id !== organization) return;
        const event = eventFromTrace(summary);
        if (event) show(event, true);
      },
      (open) => setLiveState(open ? "live" : "paused"),
    );
    return () => {
      close();
      if (refetch.current) clearTimeout(refetch.current);
    };
  }, [trace, mode, organization, show]);

  const replayEvent = useCallback((event: MapEvent) => show(event, false), [show]);
  const searchRanks = useMemo(
    () => new Map((hits ?? []).map((hit) => [hit.id, hit.rank])),
    [hits],
  );
  // Live counts belong to the snapshot they were seen on; a fresh one starts clean.
  const recallsNow = liveRecalls.on === snapshot ? liveRecalls.counts : NO_RECALLS;

  const visible = useMemo(() => applyFilters(snapshot, filters), [snapshot, filters]);
  const scale = useMemo(() => colorScale(visible.nodes, colorBy), [visible.nodes, colorBy]);
  const highlight = useMemo(() => (trace ? highlightFromTrace(trace) : null), [trace]);
  const current = organizations.find((o) => o.id === organization);
  const isEmpty = loaded && snapshot.nodes.length === 0;
  const filteredOut = loaded && !isEmpty && visible.nodes.length === 0;

  const navigate = useCallback(
    (id: string) => {
      select(id);
      setFocus({ node: id, seq: Date.now() });
    },
    [select],
  );
  // A linked memory may live in another tenant or outside the current workspace
  // filter; follow it so the pane never shows something the map is hiding.
  const follow = useCallback(
    (memory: MemoryDetail) => {
      if (memory.organization_id !== organization) {
        setOrganization(memory.organization_id);
        setWorkspace("");
        setFilters(NO_FILTERS);
      } else if (workspace && memory.workspace_id !== workspace) {
        setWorkspace("");
      }
    },
    [organization, workspace],
  );

  const changeOrganization = (id: string) => {
    setOrganization(id);
    setWorkspace("");
    setWorkspaces([]);
    setFilters(NO_FILTERS);
    setLoaded(false);
    setSnapshot(EMPTY);
    setHits(null);
    setTicker([]);
    setPulseCount(0);
    select(null);
  };
  const changeWorkspace = (id: string) => {
    setWorkspace(id);
    setFilters(NO_FILTERS);
    setHits(null);
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
      >
        <MapSearch
          organization={organization}
          workspace={workspace}
          hits={hits}
          onHits={setHits}
          onOpen={navigate}
        />
      </MapToolbar>
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
        <span className="map-stats mono">
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
          <div className="map-stage">
            <MapView
              key={`${organization}|${workspace}`}
              snapshot={visible}
              colorBy={colorBy}
              selectedId={selected}
              focus={focus}
              highlight={highlight}
              pulses={pulses}
              searchHits={searchRanks}
              liveRecalls={recallsNow}
              onSelect={select}
            />
            {trace ? null : (
              <ReplayBar
                organization={organization}
                mode={mode}
                onMode={setMode}
                liveState={liveState}
                pulseCount={pulseCount}
                ticker={ticker}
                onEvent={replayEvent}
              />
            )}
          </div>
        )}
        {selected ? (
          <ReadingPane
            memoryId={selected}
            onNavigate={navigate}
            onClose={() => select(null)}
            onLoaded={follow}
          />
        ) : null}
      </div>
    </div>
  );
}
