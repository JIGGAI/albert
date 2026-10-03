"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { listTraces, openTraceStream, relativeTime, shortId } from "@/lib/api";
import type { TraceSummary } from "@/lib/types";
import { Backends } from "./Backends";
import { StatusPill } from "./StatusPill";

const MAX_ROWS = 500;

export function Feed() {
  const [traces, setTraces] = useState<TraceSummary[]>([]);
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [text, setText] = useState("");
  const [live, setLive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [fresh, setFresh] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    let cancelled = false;
    listTraces({ limit: 100, kind: kind || undefined, status: status || undefined })
      .then((page) => {
        if (!cancelled) setTraces(page.items);
      })
      .catch((e: Error) => setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [kind, status]);

  useEffect(() => {
    const close = openTraceStream((trace) => {
      setFresh((current) => {
        const next = new Set(current).add(trace.id);
        if (next.size > MAX_ROWS) next.delete(next.values().next().value as string);
        return next;
      });
      setLive(true);
      setTraces((current) => {
        if (current.some((t) => t.id === trace.id)) return current;
        return [trace, ...current].slice(0, MAX_ROWS);
      });
    });
    const tick = setInterval(() => setNow(Date.now()), 5000);
    return () => {
      close();
      clearInterval(tick);
    };
  }, []);

  const visible = useMemo(() => {
    const needle = text.trim().toLowerCase();
    return traces.filter((trace) => {
      if (kind && trace.kind !== kind) return false;
      if (status && trace.status !== status) return false;
      if (!needle) return true;
      const query = String(trace.summary.query ?? "").toLowerCase();
      return trace.name.toLowerCase().includes(needle) || query.includes(needle);
    });
  }, [traces, kind, status, text]);

  return (
    <>
      <div className="toolbar">
        <select aria-label="Kind" value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">All kinds</option>
          <option value="request">Requests</option>
          <option value="job">Jobs</option>
        </select>
        <select aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">Any status</option>
          <option value="ok">ok</option>
          <option value="degraded">degraded</option>
          <option value="error">error</option>
        </select>
        <input
          aria-label="Filter by name or query"
          placeholder="Filter by route, job or query text"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <span className="spacer" />
        <span className="muted">
          <span className={`live-dot ${live ? "" : "off"}`} />
          {live ? "streaming" : "waiting for traces"}
        </span>
      </div>
      {error ? <div className="error-box">Console could not reach Albert: {error}</div> : null}
      <div className="table" role="table" aria-label="Recent traces">
        <div className="row head" role="row">
          <span>Status</span>
          <span>Kind</span>
          <span>Name</span>
          <span>Query</span>
          <span>Backends</span>
          <span className="right">Duration</span>
          <span>Tenant</span>
          <span className="right">When</span>
        </div>
        {visible.length === 0 ? (
          <div className="empty">
            No traces yet. Make a request to Albert and it appears here within a second.
          </div>
        ) : null}
        {visible.map((trace) => (
          <Link
            key={trace.id}
            href={`/traces/${trace.id}`}
            className={`row ${fresh.has(trace.id) ? "entering" : ""}`}
            role="row"
            data-testid="trace-row"
          >
            <StatusPill status={trace.status} code={trace.http_status} />
            <span className="kind">{trace.kind}</span>
            <span className="ellipsis mono">{trace.name}</span>
            <span className="ellipsis mono muted">{String(trace.summary.query ?? "")}</span>
            <Backends trace={trace} />
            <span className="right mono">{trace.duration_ms.toFixed(1)} ms</span>
            <span className="mono muted">{shortId(trace.organization_id)}</span>
            <span className="right muted">{relativeTime(trace.started_at, now)}</span>
          </Link>
        ))}
      </div>
    </>
  );
}
