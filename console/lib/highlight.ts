import type { Candidate, FusedHit, TraceDetail } from "./types";

export interface Highlight {
  lexical: Set<string>;
  vector: Set<string>;
  graph: Set<string>;
  hits: Set<string>;
  edgeHits: Set<string>;
}

export const EMPTY_HIGHLIGHT: Highlight = {
  lexical: new Set(),
  vector: new Set(),
  graph: new Set(),
  hits: new Set(),
  edgeHits: new Set(),
};

function candidateIds(trace: TraceDetail, name: string): Set<string> {
  const span = trace.spans.find((s) => s.name === name);
  const value = span?.detail.candidates;
  return new Set(Array.isArray(value) ? (value as Candidate[]).map((c) => c.id) : []);
}

/** Which graph nodes and edges a trace touched, by backend, plus the final hits. */
export function highlightFromTrace(trace: TraceDetail): Highlight {
  const graph = candidateIds(trace, "graph");
  const fuse = trace.spans.find((s) => s.name === "fuse");
  const hits = Array.isArray(fuse?.detail.hits) ? (fuse!.detail.hits as FusedHit[]) : [];
  return {
    lexical: candidateIds(trace, "lexical"),
    vector: candidateIds(trace, "vector"),
    graph,
    hits: new Set(hits.filter((h) => h.kind !== "relationship").map((h) => h.id)),
    edgeHits: new Set(hits.filter((h) => h.kind === "relationship").map((h) => h.id)),
  };
}

export function highlightCount(highlight: Highlight): number {
  return highlight.hits.size + highlight.edgeHits.size;
}
