"use client";

import dynamic from "next/dynamic";
import type { ForceGraphMethods } from "react-force-graph-2d";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  NEAR_DUPLICATE,
  PULSE_MS,
  clusterColor,
  colorScale,
  convexHull,
  hexPath,
  linkEnds,
  nodeRadius,
  paddedHullPath,
  type ColorBy,
  type Highlight,
  type Point,
  type Pulse,
} from "@/lib/map";
import type { LinkKind, MapNode, MapSnapshot } from "@/lib/types";

const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), { ssr: false });

type SimNode = MapNode & { x?: number; y?: number };
interface SimLink {
  source: string | SimNode;
  target: string | SimNode;
  kind: LinkKind;
  weight: number;
}
interface LinkForce {
  distance: (fn: (link: SimLink) => number) => LinkForce;
  strength: (fn: (link: SimLink) => number) => LinkForce;
}
interface ChargeForce {
  strength: (value: number) => ChargeForce;
  distanceMax: (value: number) => ChargeForce;
}

const HULL_PAD = 16;
const LABEL_ZOOM = 1.7;
const INK = "232, 237, 244";
const LINK_RGB: Record<LinkKind, string> = {
  similar: "138, 151, 170",
  sequence: "76, 195, 138",
  recalled: "255, 154, 77",
};
const PULSE_RGB = { recall: "255, 154, 77", store: "76, 195, 138" };

function withAlpha(color: string, alpha: number): string {
  if (color.startsWith("rgb(")) return color.replace("rgb(", "rgba(").replace(")", `, ${alpha})`);
  const value = parseInt(color.slice(1), 16);
  return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${alpha})`;
}

let cachedFont: string | null = null;
/** The page's text face, read once at draw time (canvas cannot use CSS variables). */
function canvasFont(): string {
  cachedFont ??= getComputedStyle(document.body).fontFamily || "sans-serif";
  return cachedFont;
}

function prefersReducedMotion(): boolean {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Reuse the layout engine's node objects so positions survive filters and refetches. */
function reuseNodes(store: Map<string, SimNode>, nodes: MapNode[]): SimNode[] {
  const seen = new Set<string>();
  const result = nodes.map((node) => {
    seen.add(node.id);
    const existing = store.get(node.id);
    if (existing) return Object.assign(existing, node);
    const created: SimNode = { ...node };
    store.set(node.id, created);
    return created;
  });
  for (const id of [...store.keys()]) if (!seen.has(id)) store.delete(id);
  return result;
}

function clip(text: string, length: number): string {
  return text.length > length ? `${text.slice(0, length - 1)}…` : text;
}

export function MapView({
  snapshot,
  colorBy,
  selectedId,
  focus,
  highlight,
  pulses,
  searchHits,
  onSelect,
}: {
  snapshot: MapSnapshot;
  colorBy: ColorBy;
  selectedId: string | null;
  /** Center the view on a node or a cluster; `seq` re-triggers the same target. */
  focus: { node?: string; cluster?: number; seq: number } | null;
  highlight: Highlight | null;
  pulses: Pulse[];
  searchHits: Map<string, number>;
  onSelect: (id: string | null) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const graph = useRef<ForceGraphMethods | undefined>(undefined);
  const store = useRef(new Map<string, SimNode>());
  const fitted = useRef(false);
  const [size, setSize] = useState({ width: 600, height: 480 });
  const [hover, setHover] = useState<{ node: SimNode; x: number; y: number } | null>(null);

  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      setSize({ width: entry.contentRect.width, height: Math.max(320, entry.contentRect.height) });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  // The force engine owns these node objects and writes x/y onto them; handing
  // the same objects back is what keeps the layout from restarting.
  const data = useMemo(
    () => ({
      // eslint-disable-next-line react-hooks/refs -- an identity cache, not render state
      nodes: reuseNodes(store.current, snapshot.nodes),
      links: snapshot.links.map((link): SimLink => ({ ...link })),
    }),
    [snapshot],
  );

  const scale = useMemo(() => colorScale(snapshot.nodes, colorBy), [snapshot.nodes, colorBy]);
  const neighbours = useMemo(() => {
    const set = new Set<string>();
    if (!selectedId) return set;
    set.add(selectedId);
    for (const link of snapshot.links) {
      if (link.source === selectedId) set.add(link.target);
      if (link.target === selectedId) set.add(link.source);
    }
    return set;
  }, [snapshot.links, selectedId]);

  useEffect(() => {
    let frame = 0;
    const apply = () => {
      const instance = graph.current;
      if (!instance) {
        frame = requestAnimationFrame(apply);
        return;
      }
      // Similar links pull hard so topics gather; the other kinds barely tug,
      // so they read as threads across the map rather than reshaping it.
      const link = instance.d3Force("link") as unknown as LinkForce | undefined;
      link
        ?.distance((l) => (l.kind === "similar" ? 26 : 70))
        .strength((l) =>
          l.kind === "similar" ? 0.25 + 0.6 * Math.max(0, (l.weight - 0.78) / 0.22) : 0.02,
        );
      const charge = instance.d3Force("charge") as unknown as ChargeForce | undefined;
      charge?.strength(-55).distanceMax(260);
      instance.d3ReheatSimulation();
    };
    apply();
    return () => cancelAnimationFrame(frame);
  }, [data]);

  useEffect(() => {
    const instance = graph.current;
    if (!focus || !instance) return;
    if (focus.node) {
      const node = store.current.get(focus.node);
      if (node?.x === undefined || node.y === undefined) return;
      instance.centerAt(node.x, node.y, 500);
      if (instance.zoom() < 2.2) instance.zoom(2.2, 500);
    } else if (focus.cluster !== undefined) {
      instance.zoomToFit(500, 90, (node) => (node as SimNode).cluster === focus.cluster);
    }
  }, [focus]);

  const dimmed = useCallback(
    (id: string) => {
      if (highlight) {
        return !(highlight.hits.has(id) || highlight.lexical.has(id) || highlight.vector.has(id));
      }
      if (searchHits.size > 0) return !searchHits.has(id) && id !== selectedId;
      return selectedId !== null && !neighbours.has(id);
    },
    [highlight, searchHits, selectedId, neighbours],
  );

  const drawNode = useCallback(
    (raw: object, ctx: CanvasRenderingContext2D, zoom: number) => {
      const node = raw as SimNode;
      if (node.x === undefined || node.y === undefined) return;
      const r = nodeRadius(node.recalls);
      const faded = dimmed(node.id);
      let fill = scale.color(node);
      if (highlight) {
        const lexical = highlight.lexical.has(node.id);
        const vector = highlight.vector.has(node.id);
        if (lexical && vector) fill = "#8ad0ff";
        else if (lexical) fill = "#5aa9ff";
        else if (vector) fill = "#c792ea";
      }
      hexPath(ctx, node.x, node.y, r);
      ctx.fillStyle = withAlpha(fill, faded ? 0.16 : 0.92);
      ctx.fill();
      if (node.sensitivity === "confidential" || node.sensitivity === "restricted") {
        hexPath(ctx, node.x, node.y, r + 2);
        ctx.lineWidth = 0.9;
        ctx.strokeStyle = withAlpha(
          node.sensitivity === "restricted" ? "#f0616d" : "#e0a93b",
          faded ? 0.25 : 0.9,
        );
        ctx.stroke();
      }
      const rank = searchHits.get(node.id);
      if (highlight?.hits.has(node.id) || rank !== undefined) {
        hexPath(ctx, node.x, node.y, r + 3.6);
        ctx.lineWidth = 1.3;
        ctx.strokeStyle = highlight ? "#ffffff" : "#5aa9ff";
        ctx.stroke();
      }
      if (node.id === selectedId) {
        hexPath(ctx, node.x, node.y, r + 3.6);
        ctx.lineWidth = 1.6;
        ctx.strokeStyle = "#ffffff";
        ctx.stroke();
      }
      if (rank !== undefined && rank <= 9) {
        const bx = node.x + r + 3;
        const by = node.y - r - 3;
        ctx.beginPath();
        ctx.arc(bx, by, 3.6, 0, Math.PI * 2);
        ctx.fillStyle = "#5aa9ff";
        ctx.fill();
        ctx.font = `600 5px ${canvasFont()}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillStyle = "#0e1420";
        ctx.fillText(String(rank), bx, by + 0.3);
      }
      const labelled =
        node.id === selectedId || rank !== undefined || (zoom >= LABEL_ZOOM && !faded);
      if (labelled) {
        ctx.font = `${11 / zoom}px ${canvasFont()}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle = `rgba(${INK}, ${node.id === selectedId ? 1 : 0.78})`;
        ctx.fillText(clip(node.title, 34), node.x, node.y + r + 3 / zoom + 2);
      }
    },
    [dimmed, scale, highlight, searchHits, selectedId],
  );

  const paintPointer = useCallback((raw: object, color: string, ctx: CanvasRenderingContext2D) => {
    const node = raw as SimNode;
    if (node.x === undefined || node.y === undefined) return;
    hexPath(ctx, node.x, node.y, nodeRadius(node.recalls) + 2.5);
    ctx.fillStyle = color;
    ctx.fill();
  }, []);

  const linkTouchesSelection = useCallback(
    (link: SimLink) => {
      if (!selectedId) return false;
      const [source, target] = linkEnds(link);
      return source === selectedId || target === selectedId;
    },
    [selectedId],
  );
  const linkColor = useCallback(
    (raw: object) => {
      const link = raw as SimLink;
      const [source, target] = linkEnds(link);
      const near = link.kind === "similar" && link.weight >= NEAR_DUPLICATE;
      let alpha =
        link.kind === "similar"
          ? near
            ? 0.8
            : 0.14 + 0.42 * Math.max(0, (link.weight - 0.78) / 0.22)
          : 0.6;
      if (linkTouchesSelection(link)) alpha = Math.min(1, alpha + 0.45);
      else if (dimmed(source) || dimmed(target)) alpha *= 0.18;
      return `rgba(${near ? INK : LINK_RGB[link.kind]}, ${alpha})`;
    },
    [dimmed, linkTouchesSelection],
  );
  const linkWidth = useCallback((raw: object) => {
    const link = raw as SimLink;
    if (link.kind === "similar") return link.weight >= NEAR_DUPLICATE ? 2 : 0.7;
    if (link.kind === "sequence") return 1;
    return Math.min(3, 0.9 + 0.5 * Math.log2(1 + link.weight));
  }, []);
  const linkDash = useCallback(
    (raw: object) => ((raw as SimLink).kind === "recalled" ? [3, 2] : null),
    [],
  );
  const linkArrow = useCallback((raw: object) => ((raw as SimLink).kind === "sequence" ? 3.5 : 0), []);
  const linkCurve = useCallback((raw: object) => ((raw as SimLink).kind === "recalled" ? 0.22 : 0), []);

  const drawHulls = useCallback(
    (ctx: CanvasRenderingContext2D, zoom: number) => {
      const groups = new Map<number, Point[]>();
      for (const node of data.nodes) {
        if (node.cluster === null || node.x === undefined || node.y === undefined) continue;
        const points = groups.get(node.cluster) ?? [];
        points.push([node.x, node.y]);
        groups.set(node.cluster, points);
      }
      for (const cluster of snapshot.clusters) {
        const points = groups.get(cluster.id);
        if (!points) continue;
        const color = clusterColor(cluster.id);
        paddedHullPath(ctx, convexHull(points), HULL_PAD);
        ctx.fillStyle = withAlpha(color, highlight ? 0.035 : 0.07);
        ctx.fill();
        ctx.lineWidth = 1 / zoom;
        ctx.strokeStyle = withAlpha(color, highlight ? 0.12 : 0.28);
        ctx.stroke();
        const top = Math.min(...points.map((p) => p[1])) - HULL_PAD - 5 / zoom;
        const center = points.reduce((sum, p) => sum + p[0], 0) / points.length;
        ctx.font = `500 ${12 / zoom}px ${canvasFont()}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "bottom";
        ctx.fillStyle = withAlpha(color, 0.95);
        ctx.fillText(cluster.label, center, top);
      }
    },
    [data.nodes, snapshot.clusters, highlight],
  );

  const drawPulses = useCallback(
    (ctx: CanvasRenderingContext2D) => {
      const now = performance.now();
      const still = prefersReducedMotion();
      for (const pulse of pulses) {
        const node = store.current.get(pulse.nodeId);
        if (!node || node.x === undefined || node.y === undefined) continue;
        const t = (now - pulse.at) / PULSE_MS;
        if (t < 0 || t > 1) continue;
        const r = nodeRadius(node.recalls);
        const rgb = PULSE_RGB[pulse.kind];
        if (still) {
          hexPath(ctx, node.x, node.y, r + 6);
          ctx.lineWidth = 2;
          ctx.strokeStyle = `rgba(${rgb}, 0.9)`;
          ctx.stroke();
          continue;
        }
        for (const offset of [0, 0.28]) {
          const phase = t - offset;
          if (phase < 0) continue;
          ctx.beginPath();
          ctx.arc(node.x, node.y, r + 3 + phase * 30, 0, Math.PI * 2);
          ctx.lineWidth = 2.2 * (1 - phase);
          ctx.strokeStyle = `rgba(${rgb}, ${0.9 * (1 - phase)})`;
          ctx.stroke();
        }
        hexPath(ctx, node.x, node.y, r + 1.5);
        ctx.fillStyle = `rgba(${rgb}, ${0.55 * (1 - t)})`;
        ctx.fill();
      }
    },
    [pulses],
  );

  const onHover = (raw: object | null) => {
    const node = raw as SimNode | null;
    const instance = graph.current;
    if (!node || !instance || node.x === undefined || node.y === undefined) {
      setHover(null);
      return;
    }
    const point = instance.graph2ScreenCoords(node.x, node.y);
    setHover({ node, x: point.x, y: point.y });
  };

  const fitOnce = useCallback(() => {
    if (fitted.current || data.nodes.length === 0) return;
    fitted.current = true;
    const touched = highlight
      ? new Set([...highlight.hits, ...highlight.lexical, ...highlight.vector])
      : null;
    graph.current?.zoomToFit(
      500,
      60,
      touched && touched.size > 0 ? (node) => touched.has((node as SimNode).id) : undefined,
    );
  }, [data.nodes.length, highlight]);

  return (
    <div ref={host} className="map-host" data-testid="map-canvas">
      <ForceGraph2D
        ref={graph}
        width={size.width}
        height={size.height}
        graphData={data}
        backgroundColor="#0e1420"
        nodeId="id"
        nodeLabel=""
        nodeCanvasObject={drawNode}
        nodePointerAreaPaint={paintPointer}
        linkColor={linkColor}
        linkWidth={linkWidth}
        linkLineDash={linkDash}
        linkCurvature={linkCurve}
        linkDirectionalArrowLength={linkArrow}
        linkDirectionalArrowRelPos={0.86}
        onRenderFramePre={drawHulls}
        onRenderFramePost={drawPulses}
        onNodeClick={(node) => onSelect((node as SimNode).id)}
        onNodeHover={onHover}
        onBackgroundClick={() => onSelect(null)}
        onZoom={() => setHover(null)}
        onEngineStop={fitOnce}
        autoPauseRedraw={pulses.length === 0}
        warmupTicks={60}
        cooldownTicks={140}
        d3AlphaDecay={0.035}
        d3VelocityDecay={0.35}
        minZoom={0.2}
        maxZoom={10}
        enableNodeDrag={false}
      />
      {hover ? (
        <div className="map-tip" style={{ left: hover.x, top: hover.y }} role="tooltip">
          <strong>{hover.node.title}</strong>
          <span>
            {[hover.node.type, hover.node.team, hover.node.role].filter(Boolean).join(" · ")}
          </span>
          <span>
            {hover.node.recalls > 0
              ? `recalled ${hover.node.recalls} ${hover.node.recalls === 1 ? "time" : "times"}`
              : "never recalled"}
          </span>
        </div>
      ) : null}
    </div>
  );
}
