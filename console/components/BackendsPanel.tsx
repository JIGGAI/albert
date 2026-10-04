"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { getBackends } from "@/lib/api";
import type { BackendOption, Backends, StoreBackend } from "@/lib/types";

const REFRESH_MS = 15_000;
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
  const hours = 24;
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

      <p className="muted">
        How the graph is being built and queried is under{" "}
        <Link href="/ops?view=graph">Operations → Graph</Link>.
      </p>

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
