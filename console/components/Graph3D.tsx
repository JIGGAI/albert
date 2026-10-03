"use client";

import dynamic from "next/dynamic";
import type { ForceGraphMethods } from "react-force-graph-3d";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Highlight } from "@/lib/highlight";
import type { GraphEdge, GraphNode, GraphSnapshot } from "@/lib/types";

const ForceGraph3D = dynamic(() => import("react-force-graph-3d"), { ssr: false });

const COLORS = {
  entity: "#8a97aa",
  public: "#4cc38a",
  internal: "#5aa9ff",
  confidential: "#e0a93b",
  restricted: "#f0616d",
  lexical: "#5aa9ff",
  vector: "#c792ea",
  both: "#8ad0ff",
  graph: "#ff9a4d",
  hit: "#ffffff",
  dim: "rgba(138,151,170,0.22)",
  link: "rgba(138,151,170,0.35)",
  linkDim: "rgba(138,151,170,0.08)",
};

type Link = GraphEdge & { source: string; target: string };

function nodeColor(node: GraphNode, highlight: Highlight | null): string {
  if (!highlight) {
    if (node.kind === "entity") return COLORS.entity;
    return COLORS[(node.sensitivity as keyof typeof COLORS) ?? "internal"] ?? COLORS.internal;
  }
  if (highlight.hits.has(node.id)) return COLORS.hit;
  const lexical = highlight.lexical.has(node.id);
  const vector = highlight.vector.has(node.id);
  if (lexical && vector) return COLORS.both;
  if (lexical) return COLORS.lexical;
  if (vector) return COLORS.vector;
  return COLORS.dim;
}

export function Graph3D({
  snapshot,
  highlight,
  onSelect,
}: {
  snapshot: GraphSnapshot;
  highlight: Highlight | null;
  onSelect: (node: GraphNode) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const graph = useRef<ForceGraphMethods | undefined>(undefined);
  const [size, setSize] = useState({ width: 600, height: 480 });

  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      setSize({ width: entry.contentRect.width, height: Math.max(360, entry.contentRect.height) });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  // Accessors are memoized so the force graph is not re-digested on every render;
  // motion comes from the edge particles the library animates itself.
  const data = useMemo(
    () => ({
      nodes: snapshot.nodes.map((node) => ({ ...node })),
      links: snapshot.edges.map((edge) => ({ ...edge }) as Link),
    }),
    [snapshot],
  );
  const color = useCallback((node: object) => nodeColor(node as GraphNode, highlight), [highlight]);
  const value = useCallback(
    (node: object) => {
      const n = node as GraphNode;
      if (highlight?.hits.has(n.id)) return 8;
      return n.kind === "entity" ? 3 : 2;
    },
    [highlight],
  );
  const isHitEdge = useCallback(
    (link: object) => {
      const l = link as Link;
      return !!highlight && (highlight.edgeHits.has(l.id) || highlight.graph.has(l.id));
    },
    [highlight],
  );
  const linkColor = useCallback(
    (link: object) => (!highlight ? COLORS.link : isHitEdge(link) ? COLORS.graph : COLORS.linkDim),
    [highlight, isHitEdge],
  );
  const linkWidth = useCallback((link: object) => (isHitEdge(link) ? 2.5 : 0.6), [isHitEdge]);
  const particles = useCallback((link: object) => (isHitEdge(link) ? 4 : 0), [isHitEdge]);
  const label = useCallback((node: object) => {
    const n = node as GraphNode;
    return `${n.kind}: ${n.label}`;
  }, []);
  const frameHighlight = useCallback(() => {
    if (!highlight || !graph.current) return;
    const touched = new Set([...highlight.hits, ...highlight.lexical, ...highlight.vector]);
    if (touched.size === 0) return;
    graph.current.zoomToFit(600, 60, (node) => touched.has((node as GraphNode).id));
  }, [highlight]);

  return (
    <div ref={host} className="graph-host" data-testid="graph-canvas">
      <ForceGraph3D
        ref={graph}
        width={size.width}
        height={size.height}
        graphData={data}
        backgroundColor="#0e1420"
        nodeId="id"
        nodeLabel={label}
        nodeColor={color}
        nodeVal={value}
        nodeOpacity={0.95}
        linkColor={linkColor}
        linkWidth={linkWidth}
        linkDirectionalParticles={particles}
        linkDirectionalParticleColor={() => COLORS.graph}
        onNodeClick={(node) => onSelect(node as GraphNode)}
        onEngineStop={frameHighlight}
        enableNodeDrag={false}
        showNavInfo={false}
      />
    </div>
  );
}
