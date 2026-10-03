"use client";

import type { Candidate, FusedHit, Span } from "@/lib/types";

const BACKENDS = ["lexical", "vector", "graph"] as const;

function candidatesOf(spans: Span[], name: string): Candidate[] {
  const span = spans.find((s) => s.name === name);
  const value = span?.detail.candidates;
  return Array.isArray(value) ? (value as Candidate[]) : [];
}

export function FusionTable({ spans }: { spans: Span[] }) {
  const fuse = spans.find((span) => span.name === "fuse");
  const hits = (fuse?.detail.hits as FusedHit[] | undefined) ?? [];
  const ranks = Object.fromEntries(
    BACKENDS.map((backend) => [
      backend,
      new Map(candidatesOf(spans, backend).map((c) => [c.id, c])),
    ]),
  ) as Record<(typeof BACKENDS)[number], Map<string, Candidate>>;
  const hitIds = new Set(hits.map((hit) => hit.id));
  const singles = BACKENDS.flatMap((backend) =>
    candidatesOf(spans, backend)
      .filter((c) => !hitIds.has(c.id) && BACKENDS.filter((b) => ranks[b].has(c.id)).length === 1)
      .map((c) => ({ ...c, backend })),
  );

  if (!fuse) return null;
  return (
    <section className="fusion" data-testid="fusion-table">
      <h2>Why these results</h2>
      <p className="muted">
        Reciprocal-rank fusion with k = {String(fuse.detail.k)}. Each backend contributes
        1 / (k + rank); the sum orders the final list.
      </p>
      <table className="data">
        <thead>
          <tr>
            <th>final</th>
            <th>id</th>
            <th className="lexical">lexical</th>
            <th className="vector">vector</th>
            <th className="graph">graph</th>
            <th className="right">rrf</th>
          </tr>
        </thead>
        <tbody>
          {hits.map((hit) => (
            <tr key={hit.id}>
              <td className="mono">{hit.rank}</td>
              <td className="mono">
                {hit.id}
                <span className="kind">{hit.kind}</span>
              </td>
              {BACKENDS.map((backend) => {
                const candidate = ranks[backend].get(hit.id);
                return (
                  <td key={backend} className={`mono ${candidate ? backend : "faint"}`}>
                    {candidate ? `#${candidate.rank}` : "—"}
                  </td>
                );
              })}
              <td className="mono right">
                {Object.values(hit.rrf).reduce((a, b) => a + b, 0).toFixed(4)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {singles.length > 0 ? (
        <>
          <h2>Found by one backend only</h2>
          <p className="muted">
            Candidates a single backend proposed that did not make the final list.
          </p>
          <ul className="singles">
            {singles.map((candidate) => (
              <li key={`${candidate.backend}:${candidate.id}`} className="mono">
                <i className={`swatch ${candidate.backend}`} /> {candidate.id} at #{candidate.rank}
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </section>
  );
}
