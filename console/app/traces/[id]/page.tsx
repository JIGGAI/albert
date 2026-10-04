"use client";

import { Suspense, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { getTrace, shortId } from "@/lib/api";
import type { TraceDetail } from "@/lib/types";
import { FusionTable } from "@/components/FusionTable";
import { SpanDetail } from "@/components/SpanDetail";
import { StatusPill } from "@/components/StatusPill";
import { Waterfall } from "@/components/Waterfall";
import { MapPanel } from "@/components/MapPanel";

export default function TracePage() {
  const { id } = useParams<{ id: string }>();
  const [trace, setTrace] = useState<TraceDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState(0);

  useEffect(() => {
    getTrace(id).then(setTrace).catch((e: Error) => setError(e.message));
  }, [id]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!trace) return;
      if (event.key === "ArrowRight") setSelected((s) => Math.min(trace.spans.length - 1, s + 1));
      if (event.key === "ArrowLeft") setSelected((s) => Math.max(0, s - 1));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [trace]);

  if (error) return <div className="error-box">Trace could not be loaded: {error}</div>;
  if (!trace) return <p className="muted">Loading trace…</p>;
  const span = trace.spans[selected] ?? trace.spans[0];

  return (
    <>
      <div className="page-head">
        <h1 className="mono">{trace.name}</h1>
        <StatusPill status={trace.status} code={trace.http_status} />
        <span className="sub mono">{trace.duration_ms.toFixed(2)} ms</span>
        <span className="sub">{new Date(trace.started_at).toLocaleString()}</span>
        <span className="sub mono">tenant {shortId(trace.organization_id) || "none"}</span>
        <span className="sub mono">principal {shortId(trace.principal_id) || "none"}</span>
      </div>
      {trace.summary.query ? (
        <p className="query mono">“{String(trace.summary.query)}”</p>
      ) : null}
      <div className="replay">
        <div className="replay-left">
          <Waterfall spans={trace.spans} selected={selected} onSelect={setSelected} />
          <label className="scrubber">
            <span className="muted">Step {selected + 1} of {trace.spans.length}</span>
            <input
              data-testid="step-scrubber"
              type="range"
              min={0}
              max={Math.max(0, trace.spans.length - 1)}
              value={selected}
              onChange={(e) => setSelected(Number(e.target.value))}
              aria-label="Step through spans"
            />
          </label>
          {span ? <SpanDetail span={span} /> : null}
          <FusionTable spans={trace.spans} />
        </div>
        <div className="replay-right">
          <Suspense fallback={null}>
            <MapPanel trace={trace} />
          </Suspense>
        </div>
      </div>
    </>
  );
}
