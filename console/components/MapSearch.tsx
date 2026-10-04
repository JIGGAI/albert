"use client";

import { useState } from "react";
import { searchMap } from "@/lib/api";
import type { MapSearchHit } from "@/lib/types";

export function MapSearch({
  organization,
  workspace,
  hits,
  onHits,
  onOpen,
}: {
  organization: string;
  workspace: string;
  hits: MapSearchHit[] | null;
  onHits: (hits: MapSearchHit[] | null) => void;
  onOpen: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const text = query.trim();
    if (!text || !organization) return;
    setBusy(true);
    setError(null);
    try {
      const result = await searchMap({
        organization_id: organization,
        workspace_id: workspace || undefined,
        query: text,
        limit: 20,
      });
      onHits(result.hits);
      setOpen(true);
      if (result.hits[0]) onOpen(result.hits[0].id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const clear = () => {
    setQuery("");
    setError(null);
    setOpen(false);
    onHits(null);
  };

  return (
    <form className="map-search" role="search" onSubmit={submit}>
      <input
        data-testid="map-search-input"
        aria-label="Search memories"
        placeholder="Search memories…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onFocus={() => setOpen(true)}
      />
      {hits ? (
        <>
          <button type="button" className="count" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
            <span data-testid="map-search-hit-count">{hits.length}</span>{" "}
            {hits.length === 1 ? "hit" : "hits"}
          </button>
          <button type="button" onClick={clear} aria-label="Clear search">
            Clear
          </button>
        </>
      ) : (
        <button type="submit" disabled={busy || !query.trim()}>
          {busy ? "Searching…" : "Search"}
        </button>
      )}
      {error ? <span className="search-error">Search failed: {error}</span> : null}
      {hits && open ? (
        <div className="search-results" data-testid="map-search-results">
          {hits.length === 0 ? (
            <p className="muted">Nothing matched. Try fewer or different words.</p>
          ) : (
            <ol>
              {hits.map((hit) => (
                <li key={hit.id}>
                  <button
                    type="button"
                    onClick={() => {
                      onOpen(hit.id);
                      setOpen(false);
                    }}
                  >
                    <span className="rank mono">{hit.rank}</span>
                    <span className="ellipsis">{hit.title}</span>
                    <span className="backends" aria-label={`found by ${hit.backends.join(", ")}`}>
                      {hit.backends.map((backend) => (
                        <i key={backend} className={backend} title={backend} />
                      ))}
                    </span>
                  </button>
                </li>
              ))}
            </ol>
          )}
        </div>
      ) : null}
    </form>
  );
}
