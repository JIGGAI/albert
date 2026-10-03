"use client";

import type { Candidate, FusedHit, Span } from "@/lib/types";

function isCandidates(value: unknown): value is Candidate[] {
  return (
    Array.isArray(value) &&
    value.length > 0 &&
    value.every((item) => item && typeof item === "object" && "rank" in item && "score" in item)
  );
}

function isHits(value: unknown): value is FusedHit[] {
  return (
    Array.isArray(value) &&
    value.length > 0 &&
    value.every((item) => item && typeof item === "object" && "rrf" in item)
  );
}

export function SpanDetail({ span }: { span: Span }) {
  const entries = Object.entries(span.detail);
  return (
    <section className="detail" data-testid="span-detail" aria-live="polite">
      <header>
        <h2>
          {span.name}
          <span className={`pill ${span.status}`}>{span.status}</span>
        </h2>
        <span className="mono muted">
          starts at {span.started_offset_ms.toFixed(2)} ms, runs {span.duration_ms.toFixed(2)} ms
        </span>
      </header>
      {entries.length === 0 ? <p className="muted">This step recorded no detail.</p> : null}
      <dl>
        {entries.map(([key, value]) => {
          if (isCandidates(value)) {
            return (
              <div key={key} className="wide">
                <dt>{key}</dt>
                <dd>
                  <table className="data">
                    <thead>
                      <tr>
                        <th>rank</th>
                        <th>id</th>
                        <th>kind</th>
                        <th className="right">score</th>
                      </tr>
                    </thead>
                    <tbody>
                      {value.map((candidate) => (
                        <tr key={candidate.id}>
                          <td className="mono">{candidate.rank}</td>
                          <td className="mono">{candidate.id}</td>
                          <td>{candidate.kind}</td>
                          <td className="mono right">{candidate.score.toFixed(4)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </dd>
              </div>
            );
          }
          if (isHits(value)) {
            return null; // rendered by FusionTable
          }
          return (
            <div key={key}>
              <dt>{key}</dt>
              <dd className="mono">
                {typeof value === "object" ? JSON.stringify(value) : String(value)}
              </dd>
            </div>
          );
        })}
      </dl>
    </section>
  );
}
