"use client";

import { useEffect, useState } from "react";
import { getBackends } from "@/lib/api";
import type { BackendOption, Backends, StoreBackend } from "@/lib/types";

const REFRESH_MS = 15_000;
const WINDOWS = [
  { hours: 1, label: "Last hour" },
  { hours: 24, label: "Last 24 hours" },
  { hours: 168, label: "Last 7 days" },
];
const number = new Intl.NumberFormat("en-US");

function ms(value: number | null): string {
  if (value === null) return "–";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value.toFixed(value < 10 ? 1 : 0)} ms`;
}

function state(option: BackendOption, reachable?: boolean): { label: string; tone: string } {
  if (!option.available) return { label: "not built yet", tone: "off" };
  if (!option.active) return { label: "available, not in use", tone: "idle" };
  if (reachable === false) return { label: "in use, unreachable", tone: "bad" };
  return { label: "in use", tone: "on" };
}

function Steps({ option }: { option: BackendOption }) {
  if (option.active) return null;
  return (
    <details className="steps">
      <summary>{option.available ? "How to turn it on" : "Status"}</summary>
      <ol>
        {option.enable.map((step) => (
          <li key={step}>{step}</li>
        ))}
      </ol>
      {option.available ? (
        <p className="muted">
          Backends are switched in the deployment&apos;s settings, not from this page: the console
          can only read.
        </p>
      ) : null}
    </details>
  );
}

function StoreCard({ store }: { store: StoreBackend }) {
  const badge = state(store, store.status?.reachable);
  return (
    <article className={`backend ${badge.tone}`} data-testid={`store-${store.id}`}>
      <header>
        <h3>{store.name}</h3>
        <span className={`badge ${badge.tone}`}>{badge.label}</span>
      </header>
      <p>{store.summary}</p>
      {store.status ? (
        <dl className="facts">
          <dt>Connection</dt>
          <dd className={store.status.reachable ? "ok" : "bad"}>
            {store.status.reachable
              ? `reachable in ${ms(store.status.latency_ms)}`
              : `unreachable (${store.status.error})`}
          </dd>
          <dt>Version</dt>
          <dd className="mono">{store.status.version ?? "–"}</dd>
          <dt>Entities</dt>
          <dd className="mono" data-testid="store-entities">
            {store.counts ? number.format(store.counts.entities) : "–"}
          </dd>
          <dt>Live edges</dt>
          <dd className="mono" data-testid="store-edges">
            {store.counts ? number.format(store.counts.edges) : "–"}
          </dd>
        </dl>
      ) : null}
      <p className="muted when">{store.choose_when}</p>
      <Steps option={store} />
    </article>
  );
}

export function BackendsPanel() {
  const [hours, setHours] = useState(24);
  const [loaded, setLoaded] = useState<{ hours: number; data: Backends } | null>(null);
  const [failure, setFailure] = useState<{ hours: number; message: string } | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const timer = setInterval(() => setTick((n) => n + 1), REFRESH_MS);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    let cancelled = false;
    getBackends({ hours })
      .then((data) => {
        if (!cancelled) setLoaded({ hours, data });
      })
      .catch((e: Error) => {
        if (!cancelled) setFailure({ hours, message: e.message });
      });
    return () => {
      cancelled = true;
    };
  }, [hours, tick]);

  const data = loaded?.hours === hours ? loaded.data : null;
  const error = failure?.hours === hours && !data ? failure.message : null;
  if (error) return <div className="error-box">Backends could not be loaded: {error}</div>;
  if (!data) return <p className="muted">Loading…</p>;

  const activity = data.activity;
  const peak = Math.max(1, ...activity.series.map((bucket) => bucket.edges_written));
  const builder = data.builders.find((b) => b.active);
  const failures =
    activity.indexing_failures + activity.extraction_failures + activity.write_failures;
  const embeddedShare = data.vectors.memories_total
    ? Math.round((data.vectors.memories_embedded / data.vectors.memories_total) * 100)
    : 0;

  return (
    <div className="ops backends">
      <section>
        <div className="section-head">
          <h2>Graph store</h2>
          <span className="muted">where entities and relationships are kept</span>
        </div>
        <div className="backend-grid">
          {data.stores.map((store) => (
            <StoreCard key={store.id} store={store} />
          ))}
        </div>
      </section>

      <section>
        <div className="section-head">
          <h2>Graph builder</h2>
          <span className="muted">what turns memory text into entities and relationships</span>
        </div>
        <div className="backend-grid">
          {data.builders.map((option) => {
            const badge = state(option);
            return (
              <article
                key={option.id}
                className={`backend ${badge.tone}`}
                data-testid={`builder-${option.id}`}
              >
                <header>
                  <h3>{option.name}</h3>
                  <span className={`badge ${badge.tone}`}>{badge.label}</span>
                </header>
                <p>{option.summary}</p>
                {option.active && option.model ? (
                  <dl className="facts">
                    <dt>Model</dt>
                    <dd className="mono">{option.model}</dd>
                  </dl>
                ) : null}
                <p className="muted when">{option.choose_when}</p>
                <Steps option={option} />
              </article>
            );
          })}
        </div>
      </section>

      <section>
        <div className="section-head">
          <h2>Is it working</h2>
          <span className="muted">
            {builder?.name ?? "the builder"} writing to{" "}
            {data.stores.find((s) => s.active)?.name ?? "the store"}
          </span>
          <span className="spacer" />
          <select aria-label="Window" value={hours} onChange={(e) => setHours(Number(e.target.value))}>
            {WINDOWS.map((w) => (
              <option key={w.hours} value={w.hours}>
                {w.label}
              </option>
            ))}
          </select>
        </div>
        <div className="stats">
          <div className="stat">
            <span className="label">Memories indexed</span>
            <span className="value" data-testid="stat-indexed">
              {number.format(activity.indexing_jobs)}
            </span>
          </div>
          <div className="stat">
            <span className="label">Relationships written</span>
            <span className="value" data-testid="stat-edges-written">
              {number.format(activity.edges_written)}
            </span>
            <span className="note">{number.format(activity.edges_closed)} superseded</span>
          </div>
          <div className={`stat${failures ? " bad" : ""}`}>
            <span className="label">Failures</span>
            <span className="value">{number.format(failures)}</span>
            <span className="note">indexing, extraction or graph writes</span>
          </div>
          <div className="stat">
            <span className="label">Extraction time</span>
            <span className="value">{ms(activity.classify_p50_ms)}</span>
            <span className="note">95th {ms(activity.classify_p95_ms)}</span>
          </div>
          <div className="stat">
            <span className="label">Graph write time</span>
            <span className="value">{ms(activity.write_p50_ms)}</span>
            <span className="note">95th {ms(activity.write_p95_ms)}</span>
          </div>
          <div className={`stat${activity.graph_failures ? " bad" : ""}`}>
            <span className="label">Graph lookups in searches</span>
            <span className="value" data-testid="stat-graph-queries">
              {number.format(activity.graph_queries)}
            </span>
            <span className="note">
              median {ms(activity.graph_query_p50_ms)} ·{" "}
              {number.format(activity.graph_candidates)} edges found
            </span>
          </div>
        </div>
        <div className="card">
          <h2>Relationships written over time</h2>
          <figure className="columns">
            <div className="plot" role="img" aria-label="Relationships written over time">
              {activity.series.map((bucket) => (
                <div
                  key={bucket.bucket}
                  className="column"
                  title={`${new Date(bucket.bucket).toLocaleString()}: ${bucket.edges_written} written`}
                >
                  {bucket.edges_written > 0 ? (
                    <i className="graph" style={{ height: `${(bucket.edges_written / peak) * 100}%` }} />
                  ) : null}
                </div>
              ))}
            </div>
            <figcaption>
              <span>{new Date(activity.series[0].bucket).toLocaleString([], { month: "short", day: "numeric", hour: "numeric" })}</span>
              <span className="muted">peak {peak}</span>
              <span>now</span>
            </figcaption>
          </figure>
          {activity.truncated ? (
            <p className="notice">This window holds more traces than are read; figures cover the newest.</p>
          ) : null}
        </div>
      </section>

      <section>
        <div className="section-head">
          <h2>Vector store</h2>
          <span className="muted">embeddings in PostgreSQL (pgvector); always on</span>
        </div>
        <div className="card">
          <dl className="config">
            <dt>Embeddings</dt>
            <dd className="mono">
              {data.vectors.provider} · {data.vectors.model} · {data.vectors.dimensions} dimensions
            </dd>
            <dt>Memories embedded</dt>
            <dd className="mono">
              {number.format(data.vectors.memories_embedded)} of{" "}
              {number.format(data.vectors.memories_total)} ({embeddedShare}%)
            </dd>
            <dt>Chunks indexed</dt>
            <dd className="mono">{number.format(data.vectors.chunks)}</dd>
          </dl>
        </div>
      </section>
    </div>
  );
}
