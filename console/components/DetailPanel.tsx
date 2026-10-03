"use client";

import { useEffect, useState } from "react";
import { getEntity, getMemory } from "@/lib/api";
import type { GraphNode } from "@/lib/types";

function NodeDetail({ node }: { node: GraphNode }) {
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = node.kind === "memory" ? getMemory(node.id) : getEntity(node.id);
    load
      .then((data) => {
        if (!cancelled) setDetail(data);
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [node]);

  if (error) return <p className="error-box">{error}</p>;
  if (!detail) return <p className="muted">Loading…</p>;
  return (
    <dl>
      {Object.entries(detail)
        .filter(([key]) => !["id", "metadata"].includes(key))
        .map(([key, value]) => (
          <div key={key}>
            <dt>{key.replaceAll("_", " ")}</dt>
            <dd className={typeof value === "string" && value.length < 40 ? "mono" : ""}>
              {value === null ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value)}
            </dd>
          </div>
        ))}
    </dl>
  );
}

export function DetailPanel({ node, onClose }: { node: GraphNode | null; onClose: () => void }) {
  if (!node) return null;
  return (
    <aside className="detail-panel" aria-label="Selected node">
      <header>
        <h2>
          {node.kind} <span className="muted">{node.type}</span>
        </h2>
        <button type="button" onClick={onClose} aria-label="Close details">
          Close
        </button>
      </header>
      <p className="mono muted">{node.id}</p>
      <NodeDetail key={node.id} node={node} />
    </aside>
  );
}
