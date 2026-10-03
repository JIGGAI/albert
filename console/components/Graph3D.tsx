"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo, useRef, useState } from "react";
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
  const [size, setSize] = useState({ width: 600, height: 480 });
  const [pulse, setPulse] = useState(0);

  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      setSize({ width: entry.contentRect.width, height: Math.max(360, entry.contentRect.height) });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const animate = !!highlight && !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  useEffect(() => {
    if (!animate) return;
    const timer = setInterval(() => setPulse((p) => (p + 1) % 60), 100);
    return () => clearInterval(timer);
  }, [animate]);

  const data = useMemo(
    () => ({
      nodes: snapshot.nodes.map((node) => ({ ...node })),
      links: snapshot.edges.map((edge) => ({ ...edge }) as Link),
    }),
    [snapshot],
  );
  const wave = 1 + 0.6 * Math.sin((pulse / 60) * Math.PI * 2);

  return (
    <div ref={host} className="graph-host" data-testid="graph-canvas">
      <ForceGraph3D
        width={size.width}
        height={size.height}
        graphData={data}
        backgroundColor="#0e1420"
        nodeId="id"
        nodeLabel={(node) => `${(node as GraphNode).kind}: ${(node as GraphNode).label}`}
        nodeColor={(node) => nodeColor(node as GraphNode, highlight)}
        nodeVal={(node) => {
          const n = node as GraphNode;
          if (highlight?.hits.has(n.id)) return 6 * wave + 2;
          return n.kind === "entity" ? 3 : 2;
        }}
        nodeOpacity={0.95}
        linkColor={(link) => {
          const l = link as Link;
          if (!highlight) return COLORS.link;
          return highlight.edgeHits.has(l.id) || highlight.graph.has(l.id) ? COLORS.graph : COLORS.linkDim;
        }}
        linkWidth={(link) => (highlight?.edgeHits.has((link as Link).id) ? 2.5 : 0.6)}
        linkDirectionalParticles={(link) => (highlight?.edgeHits.has((link as Link).id) ? 4 : 0)}
        linkDirectionalParticleColor={() => COLORS.graph}
        onNodeClick={(node) => onSelect(node as GraphNode)}
        enableNodeDrag={false}
        showNavInfo={false}
      />
    </div>
  );
}
