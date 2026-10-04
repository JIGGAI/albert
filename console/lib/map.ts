import type {
  LinkKind,
  MapCluster,
  MapLink,
  MapNode,
  MapSnapshot,
  TraceDetail,
  TraceSummary,
} from "./types";

export type ColorBy = "type" | "team" | "sensitivity" | "age";
export const COLOR_BY: { value: ColorBy; label: string }[] = [
  { value: "type", label: "Type" },
  { value: "team", label: "Team" },
  { value: "sensitivity", label: "Sensitivity" },
  { value: "age", label: "Age" },
];
export const LINK_KINDS: { kind: LinkKind; label: string; hint: string }[] = [
  { kind: "similar", label: "Similar", hint: "about the same thing" },
  { kind: "sequence", label: "Sequence", hint: "the next dated entry" },
  { kind: "recalled", label: "Recalled together", hint: "returned by the same search" },
];
/** A similar pair at or above this is a near duplicate; drawn heavier, same kind. */
export const NEAR_DUPLICATE = 0.97;
export const PULSE_MS = 1600;

export const PALETTE = [
  "#5aa9ff",
  "#c792ea",
  "#4cc38a",
  "#e0a93b",
  "#59d0d0",
  "#f08fb0",
  "#b8c46b",
  "#ff9a4d",
  "#9aa7ff",
];
const OTHER = "#8a97aa";
const SENSITIVITY: Record<string, string> = {
  public: "#4cc38a",
  internal: "#5aa9ff",
  confidential: "#e0a93b",
  restricted: "#f0616d",
};

export interface Filters {
  team: string;
  role: string;
  type: string;
  kinds: Record<LinkKind, boolean>;
  recalledOnly: boolean;
}

export const NO_FILTERS: Filters = {
  team: "",
  role: "",
  type: "",
  kinds: { similar: true, sequence: true, recalled: true },
  recalledOnly: false,
};

export function distinct(nodes: MapNode[], key: "team" | "role" | "type"): string[] {
  return [...new Set(nodes.map((node) => node[key]).filter((v): v is string => !!v))].sort();
}

/** The part of a snapshot the current filters leave visible. */
export function applyFilters(snapshot: MapSnapshot, filters: Filters): MapSnapshot {
  const nodes = snapshot.nodes.filter(
    (node) =>
      (!filters.team || node.team === filters.team) &&
      (!filters.role || node.role === filters.role) &&
      (!filters.type || node.type === filters.type) &&
      (!filters.recalledOnly || node.recalls > 0),
  );
  const present = new Set(nodes.map((node) => node.id));
  const links = snapshot.links.filter(
    (link) => filters.kinds[link.kind] && present.has(link.source) && present.has(link.target),
  );
  const sizes = new Map<number, number>();
  for (const node of nodes) {
    if (node.cluster !== null) sizes.set(node.cluster, (sizes.get(node.cluster) ?? 0) + 1);
  }
  const clusters: MapCluster[] = snapshot.clusters
    .filter((cluster) => sizes.has(cluster.id))
    .map((cluster) => ({ ...cluster, size: sizes.get(cluster.id) ?? 0 }));
  return { nodes, links, clusters, truncated: snapshot.truncated };
}

function mix(from: [number, number, number], to: [number, number, number], t: number): string {
  const channel = (i: number) => Math.round(from[i] + (to[i] - from[i]) * t);
  return `rgb(${channel(0)}, ${channel(1)}, ${channel(2)})`;
}

export interface ColorScale {
  color: (node: MapNode) => string;
  legend: { label: string; color: string }[];
}

/** Node fill for the chosen dimension, with the legend that explains it. */
export function colorScale(nodes: MapNode[], colorBy: ColorBy): ColorScale {
  if (colorBy === "sensitivity") {
    const present = Object.keys(SENSITIVITY).filter((s) => nodes.some((n) => n.sensitivity === s));
    return {
      color: (node) => SENSITIVITY[node.sensitivity] ?? OTHER,
      legend: present.map((label) => ({ label, color: SENSITIVITY[label] })),
    };
  }
  if (colorBy === "age") {
    const times = nodes.map((node) => new Date(node.created_at).getTime());
    const min = Math.min(...times);
    const span = Math.max(1, Math.max(...times) - min);
    const old: [number, number, number] = [62, 78, 104];
    const fresh: [number, number, number] = [150, 214, 255];
    return {
      color: (node) => mix(old, fresh, (new Date(node.created_at).getTime() - min) / span),
      legend: [
        { label: "oldest", color: mix(old, fresh, 0) },
        { label: "newest", color: mix(old, fresh, 1) },
      ],
    };
  }
  const key = colorBy;
  const counts = new Map<string, number>();
  for (const node of nodes) {
    const value = node[key];
    if (value) counts.set(value, (counts.get(value) ?? 0) + 1);
  }
  // Colors follow the alphabet, not frequency, so a value keeps its color as data grows.
  const named = [...counts.keys()].sort().slice(0, PALETTE.length);
  const colors = new Map(named.map((value, index) => [value, PALETTE[index]]));
  const legend = named.map((label) => ({ label, color: colors.get(label) as string }));
  if (nodes.some((node) => !node[key] || !colors.has(node[key] as string))) {
    legend.push({ label: key === "team" ? "no team" : "other", color: OTHER });
  }
  return { color: (node) => colors.get(node[key] ?? "") ?? OTHER, legend };
}

export function clusterColor(id: number): string {
  return PALETTE[id % PALETTE.length];
}

/** Radius in map units; grows with the log of how often the memory was recalled. */
export function nodeRadius(recalls: number): number {
  return Math.min(13, 4 + 1.9 * Math.log2(1 + recalls));
}

export function hexPath(ctx: CanvasRenderingContext2D, x: number, y: number, r: number): void {
  ctx.beginPath();
  for (let i = 0; i < 6; i += 1) {
    const angle = (Math.PI / 3) * i - Math.PI / 2;
    const px = x + r * Math.cos(angle);
    const py = y + r * Math.sin(angle);
    if (i === 0) ctx.moveTo(px, py);
    else ctx.lineTo(px, py);
  }
  ctx.closePath();
}

export type Point = [number, number];

/** Convex hull (monotone chain), counter-clockwise in the canvas's own coordinates. */
export function convexHull(points: Point[]): Point[] {
  const sorted = [...points].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  if (sorted.length < 3) return sorted;
  const cross = (o: Point, a: Point, b: Point) =>
    (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower: Point[] = [];
  for (const point of sorted) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], point) <= 0) {
      lower.pop();
    }
    lower.push(point);
  }
  const upper: Point[] = [];
  for (const point of [...sorted].reverse()) {
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], point) <= 0) {
      upper.pop();
    }
    upper.push(point);
  }
  return [...lower.slice(0, -1), ...upper.slice(0, -1)];
}

/** Trace the hull grown outward by `pad`, with round corners, as one closed path. */
export function paddedHullPath(ctx: CanvasRenderingContext2D, hull: Point[], pad: number): void {
  ctx.beginPath();
  if (hull.length === 1) {
    ctx.arc(hull[0][0], hull[0][1], pad, 0, Math.PI * 2);
    ctx.closePath();
    return;
  }
  const count = hull.length;
  const normal = (from: Point, to: Point) => Math.atan2(-(to[0] - from[0]), to[1] - from[1]);
  for (let i = 0; i < count; i += 1) {
    const previous = hull[(i - 1 + count) % count];
    const current = hull[i];
    const next = hull[(i + 1) % count];
    ctx.arc(current[0], current[1], pad, normal(previous, current), normal(current, next));
  }
  ctx.closePath();
}

export interface Pulse {
  key: string;
  nodeId: string;
  kind: "recall" | "store";
  at: number;
}

export interface MapEvent {
  traceId: string;
  kind: "recall" | "store";
  ids: string[];
  at: string;
  label: string;
}

const RECALL_NAMES = new Set(["POST /v1/search", "POST /v1/context/assemble"]);

/** What a trace means for the map: which memories were recalled or stored. */
export function eventFromTrace(trace: TraceSummary): MapEvent | null {
  const recalled = trace.summary.memory_ids;
  if (RECALL_NAMES.has(trace.name) && Array.isArray(recalled) && recalled.length > 0) {
    return {
      traceId: trace.id,
      kind: "recall",
      ids: recalled,
      at: trace.started_at,
      label: trace.summary.query ? `“${trace.summary.query}”` : "search",
    };
  }
  const stored = trace.summary.stored_ids;
  if (Array.isArray(stored) && stored.length > 0) {
    return {
      traceId: trace.id,
      kind: "store",
      ids: stored,
      at: trace.started_at,
      label: trace.kind === "job" ? "indexed" : "stored",
    };
  }
  return null;
}

export interface Highlight {
  lexical: Set<string>;
  vector: Set<string>;
  hits: Set<string>;
}

function candidateIds(trace: TraceDetail, name: string): Set<string> {
  const value = trace.spans.find((span) => span.name === name)?.detail.candidates;
  return new Set(
    Array.isArray(value) ? (value as { id: string; kind: string }[]).map((c) => c.id) : [],
  );
}

/** Which memories a replayed trace touched, by backend, plus its final hits. */
export function highlightFromTrace(trace: TraceDetail): Highlight {
  const fuse = trace.spans.find((span) => span.name === "fuse");
  const hits = Array.isArray(fuse?.detail.hits)
    ? (fuse.detail.hits as { id: string; kind: string }[])
    : [];
  return {
    lexical: candidateIds(trace, "lexical"),
    vector: candidateIds(trace, "vector"),
    hits: new Set(hits.filter((hit) => hit.kind !== "relationship").map((hit) => hit.id)),
  };
}

export function linkEnds(link: MapLink | { source: unknown; target: unknown }): [string, string] {
  const id = (end: unknown) =>
    typeof end === "object" && end !== null ? String((end as { id: string }).id) : String(end);
  return [id(link.source), id(link.target)];
}
