"use client";

import Link from "next/link";
import { useState } from "react";
import { searchMap } from "@/lib/api";
import type { ConsoleSearchResult, Organization } from "@/lib/types";

function ms(value: number): string {
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value.toFixed(value < 10 ? 2 : 1)} ms`;
}

/** Run one search as an operator and show both what came back and how. */
export function SearchProbe({
  organization,
  organizations,
}: {
  organization: string;
  organizations: Organization[];
}) {
  const [query, setQuery] = useState("");
  const [withGraph, setWithGraph] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<
    (ConsoleSearchResult & { query: string; organization: string }) | null
  >(null);
  const tenantName = organizations.find((o) => o.id === organization)?.name;

  const run = async (event: React.FormEvent) => {
    event.preventDefault();
    const text = query.trim();
    if (!text || !organization) return;
    setBusy(true);
    setError(null);
    try {
      const response = await searchMap({
        organization_id: organization,
        query: text,
        limit: 10,
        include_graph: withGraph,
      });
      setResult({ ...response, query: text, organization });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const shown = result?.organization === organization ? result : null;
  const span = shown
    ? Math.max(0.001, ...shown.path.map((step) => step.started_offset_ms + step.duration_ms))
    : 1;
  const first = shown ? Math.min(...shown.path.map((step) => step.started_offset_ms), span) : 0;

  return (
    <section className="card probe" data-testid="search-probe">
      <h2>Try a search</h2>
      <form onSubmit={run} role="search">
        <input
          type="text"
          data-testid="probe-input"
          aria-label="Search query to try"
          placeholder={tenantName ? `Search ${tenantName} the way an agent would…` : "Pick a tenant first"}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <label className="check" title="Also look up entities and relationships, as agent searches do">
          <input type="checkbox" checked={withGraph} onChange={(e) => setWithGraph(e.target.checked)} />
          Include the graph
        </label>
        <button type="submit" disabled={busy || !query.trim() || !organization}>
          {busy ? "Searching…" : "Run"}
        </button>
      </form>
      <p className="summary">
        Searches <b>{tenantName ?? "the selected tenant"}</b> with operator access across every
        sensitivity. It is audited, and is not counted in the figures below or in recall
        statistics.
      </p>
      {error ? <div className="error-box">The search failed: {error}</div> : null}
      {shown ? (
        <div className="probe-result">
          <div>
            <h3>The path it took</h3>
            <ol className="path" data-testid="probe-path">
              {shown.path.map((step) => (
                <li key={step.name}>
                  <span className={`mono ${step.name}`}>{step.name}</span>
                  <span className="lane">
                    <i
                      className={step.status === "error" ? "error" : step.name}
                      style={{
                        left: `${((step.started_offset_ms - first) / (span - first)) * 100}%`,
                        width: `${(step.duration_ms / (span - first)) * 100}%`,
                      }}
                    />
                  </span>
                  <span className="mono right">{ms(step.duration_ms)}</span>
                  <span className="muted">
                    {step.status === "error"
                      ? `failed (${step.error ?? "error"})`
                      : step.name === "fuse"
                        ? `${step.candidates} kept`
                        : `${step.candidates} found`}
                  </span>
                </li>
              ))}
            </ol>
            <p className="summary">
              <span data-testid="probe-total">{ms(shown.duration_ms)}</span> in total
              {shown.degraded.length ? `; degraded: ${shown.degraded.join("; ")}` : ""}.{" "}
              {shown.trace_id ? (
                <Link href={`/traces/${shown.trace_id}`}>Open the full replay</Link>
              ) : null}
            </p>
          </div>
          <div>
            <h3>
              What “{shown.query}” returned (<span data-testid="probe-hit-count">{shown.hits.length}</span>)
            </h3>
            {shown.hits.length === 0 ? (
              <p className="muted">Nothing. No backend found a match for these words.</p>
            ) : (
              <table className="data">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Result</th>
                    <th>Found by</th>
                    <th className="right">Score</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.hits.map((hit) => (
                    <tr key={`${hit.kind}-${hit.id}`} data-testid="probe-hit">
                      <td className="mono faint">{hit.rank}</td>
                      <td>
                        {hit.memory_id ? (
                          <Link href={`/map?memory=${hit.memory_id}`}>{hit.title}</Link>
                        ) : (
                          hit.title
                        )}{" "}
                        <span className="kind">
                          {hit.kind === "relationship" ? "relationship" : hit.type}
                        </span>
                      </td>
                      <td>
                        {hit.backends.map((backend) => (
                          <span key={backend} className={`mono ${backend}`}>
                            {backend}{" "}
                          </span>
                        ))}
                      </td>
                      <td className="right mono">{hit.score.toFixed(4)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      ) : null}
    </section>
  );
}
