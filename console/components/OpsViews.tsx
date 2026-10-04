import Link from "next/link";
import { relativeTime } from "@/lib/api";
import type {
  AgentActivity,
  CountLabel,
  MemoryHealth,
  RetrievalQuality,
  ServiceHealth,
} from "@/lib/types";

const number = new Intl.NumberFormat("en-US");

function percent(value: number): string {
  return `${(value * 100).toFixed(value > 0 && value < 0.1 ? 1 : 0)}%`;
}

function ms(value: number | null): string {
  if (value === null) return "–";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${value.toFixed(value < 10 ? 1 : 0)} ms`;
}

function bytes(value: number): string {
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)} GB`;
  return `${(value / 1e6).toFixed(0)} MB`;
}

function ago(iso: string | null): string {
  return iso ? relativeTime(iso) : "never";
}

function Stat({
  id,
  label,
  value,
  note,
  tone,
}: {
  id: string;
  label: string;
  value: string;
  note?: string;
  tone?: "warn" | "bad";
}) {
  return (
    <div className={`stat${tone ? ` ${tone}` : ""}`}>
      <span className="label">{label}</span>
      <span className="value" data-testid={`stat-${id}`}>
        {value}
      </span>
      {note ? <span className="note">{note}</span> : null}
    </div>
  );
}

interface Series {
  key: string;
  label: string;
  parts: { name: string; value: number }[];
}

/** Stacked columns over time; each column's parts are named in its tooltip. */
function Columns({ series, legend }: { series: Series[]; legend: string[] }) {
  const peak = Math.max(1, ...series.map((s) => s.parts.reduce((sum, p) => sum + p.value, 0)));
  return (
    <figure className="columns">
      <div className="plot" role="img" aria-label={`${legend.join(" and ")} over time`}>
        {series.map((column) => (
          <div
            key={column.key}
            className="column"
            title={`${column.label}: ${column.parts.map((p) => `${p.value} ${p.name}`).join(", ")}`}
          >
            {column.parts.map((part) =>
              part.value > 0 ? (
                <i
                  key={part.name}
                  className={part.name}
                  style={{ height: `${(part.value / peak) * 100}%` }}
                />
              ) : null,
            )}
          </div>
        ))}
      </div>
      <figcaption>
        <span>{series[0]?.label}</span>
        <span className="keys">
          {legend.map((name) => (
            <span key={name}>
              <i className={`swatch ${name}`} /> {name}
            </span>
          ))}
          <span className="muted">peak {peak}</span>
        </span>
        <span>{series[series.length - 1]?.label}</span>
      </figcaption>
    </figure>
  );
}

function Distribution({ title, items }: { title: string; items: CountLabel[] }) {
  const peak = Math.max(1, ...items.map((item) => item.count));
  return (
    <section className="card">
      <h2>{title}</h2>
      {items.length === 0 ? <p className="muted">Nothing yet.</p> : null}
      <ul className="dist">
        {items.map((item) => (
          <li key={item.label}>
            <span className="ellipsis">{item.label}</span>
            <span className="track">
              <i style={{ width: `${(item.count / peak) * 100}%` }} />
            </span>
            <span className="mono">{number.format(item.count)}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Capped({ show }: { show: boolean }) {
  return show ? (
    <p className="notice">
      This window holds more traces than one view reads; figures cover the most recent ones.
    </p>
  ) : null;
}

export function AgentsView({ data }: { data: AgentActivity }) {
  const searches = data.principals.reduce((sum, p) => sum + p.searches, 0);
  const stores = data.principals.reduce((sum, p) => sum + p.stores, 0);
  const errors = data.principals.reduce((sum, p) => sum + p.errors, 0);
  const active = data.principals.filter((p) => p.searches + p.stores > 0).length;
  const daily = data.bucket_hours >= 24;
  const series = data.series.map((bucket) => {
    const at = new Date(bucket.bucket);
    return {
      key: bucket.bucket,
      label: daily
        ? at.toLocaleDateString([], { month: "short", day: "numeric" })
        : at.toLocaleTimeString([], { hour: "numeric" }),
      parts: [
        { name: "recall", value: bucket.recalls },
        { name: "store", value: bucket.stores },
      ],
    };
  });
  return (
    <div data-testid="ops-agents" className="ops-view">
      <div className="stats">
        <Stat id="active" label="Active principals" value={String(active)} note="searched or stored" />
        <Stat id="searches" label="Searches" value={number.format(searches)} />
        <Stat id="stores" label="Stores" value={number.format(stores)} />
        <Stat
          id="errors"
          label="Failed requests"
          value={number.format(errors)}
          tone={errors > 0 ? "warn" : undefined}
        />
      </div>
      <section className="card">
        <h2>Recalls and stores over time</h2>
        <Columns series={series} legend={["recall", "store"]} />
      </section>
      <section className="card">
        <h2>By principal</h2>
        {data.principals.length === 0 ? (
          <p className="muted">No principal made a request in this window.</p>
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th>Principal</th>
                <th className="right">searches</th>
                <th className="right">found nothing</th>
                <th className="right">avg hits</th>
                <th className="right">stores</th>
                <th className="right">failed</th>
                <th className="right">last seen</th>
              </tr>
            </thead>
            <tbody>
              {data.principals.map((p) => (
                <tr key={p.id} data-testid="agent-row">
                  <td>
                    {p.name} {p.type ? <span className="kind">{p.type}</span> : null}
                  </td>
                  <td className="right mono">{number.format(p.searches)}</td>
                  <td className={`right mono${p.zero_hit_searches ? " warn" : " faint"}`}>
                    {p.zero_hit_searches}
                  </td>
                  <td className="right mono">{p.searches ? p.avg_hits.toFixed(1) : "–"}</td>
                  <td className="right mono">{number.format(p.stores)}</td>
                  <td className={`right mono${p.errors ? " warn" : " faint"}`}>{p.errors}</td>
                  <td className="right muted">{ago(p.last_seen)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <Capped show={data.truncated} />
    </div>
  );
}

export function MemoryView({ data }: { data: MemoryHealth }) {
  const share = (count: number) => (data.total ? `${percent(count / data.total)} of all` : undefined);
  const added = data.added.map((day) => ({
    key: day.day,
    label: new Date(`${day.day}T00:00:00Z`).toLocaleDateString([], {
      month: "short",
      day: "numeric",
      timeZone: "UTC",
    }),
    parts: [{ name: "store", value: day.count }],
  }));
  return (
    <div data-testid="ops-memory" className="ops-view">
      <div className="stats">
        <Stat id="memories" label="Memories" value={number.format(data.total)} />
        <Stat
          id="never-recalled"
          label="Never recalled"
          value={number.format(data.never_recalled)}
          note={share(data.never_recalled)}
        />
        <Stat
          id="unlinked"
          label="Not linked to anything"
          value={number.format(data.unlinked)}
          note={share(data.unlinked)}
        />
        <Stat
          id="duplicates"
          label="Near-duplicate pairs"
          value={number.format(data.near_duplicate_pairs)}
          tone={data.near_duplicate_pairs > 0 ? "warn" : undefined}
        />
        <Stat
          id="unindexed"
          label="Waiting to be indexed"
          value={number.format(data.unindexed)}
          tone={data.unindexed > 0 ? "warn" : undefined}
        />
      </div>
      <section className="card">
        <h2>Added per day, last 30 days</h2>
        <Columns series={added} legend={["store"]} />
      </section>
      <div className="grid-3">
        <Distribution title="By type" items={data.by_type} />
        <Distribution title="By sensitivity" items={data.by_sensitivity} />
        <Distribution
          title="By workspace"
          items={data.by_workspace.map((w) => ({ label: w.name, count: w.count }))}
        />
      </div>
      <div className="grid-2">
        <section className="card" data-testid="top-recalled">
          <h2>Most recalled</h2>
          {data.top_recalled.length === 0 ? (
            <p className="muted">No agent search has returned a memory yet.</p>
          ) : (
            <ul className="rows">
              {data.top_recalled.map((memory) => (
                <li key={memory.id}>
                  <Link href={`/map?memory=${memory.id}`} className="ellipsis">
                    {memory.title}
                  </Link>
                  <span className="mono">{memory.recalls}×</span>
                  <span className="muted">{ago(memory.last)}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
        <section className="card">
          <h2>Near duplicates</h2>
          {data.near_duplicates.length === 0 ? (
            <p className="muted">No two memories are close enough to be the same thing.</p>
          ) : (
            <ul className="rows pairs">
              {data.near_duplicates.map((pair) => (
                <li key={`${pair.a.id}-${pair.b.id}`}>
                  <span className="pair">
                    <Link href={`/map?memory=${pair.a.id}`} className="ellipsis">
                      {pair.a.title}
                    </Link>
                    <Link href={`/map?memory=${pair.b.id}`} className="ellipsis">
                      {pair.b.title}
                    </Link>
                  </span>
                  <span className="mono">{Math.round(pair.similarity * 100)}%</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}

export function RetrievalView({ data }: { data: RetrievalQuality }) {
  return (
    <div data-testid="ops-retrieval" className="ops-view">
      <div className="stats">
        <Stat id="searches" label="Searches" value={number.format(data.searches)} />
        <Stat
          id="zero-hit"
          label="Found nothing"
          value={percent(data.zero_hit_rate)}
          note={`${data.zero_hit} of ${data.searches}`}
          tone={data.zero_hit_rate > 0.2 ? "warn" : undefined}
        />
        <Stat id="avg-hits" label="Hits per search" value={data.avg_hits.toFixed(1)} />
        <Stat id="p50" label="Typical latency" value={ms(data.p50_ms)} note="median" />
        <Stat id="p95" label="Slow latency" value={ms(data.p95_ms)} note="95th percentile" />
        <Stat
          id="degraded"
          label="Degraded"
          value={percent(data.degraded_rate)}
          note="a backend was unavailable"
          tone={data.degraded > 0 ? "warn" : undefined}
        />
      </div>
      <section className="card">
        <h2>Which backend earns the hits</h2>
        <table className="data">
          <thead>
            <tr>
              <th>Backend</th>
              <th>found the hit</th>
              <th className="right">only this one found it</th>
              <th className="right">median</th>
              <th className="right">95th</th>
              <th className="right">failures</th>
            </tr>
          </thead>
          <tbody>
            {data.backends.map((backend) => (
              <tr key={backend.name} data-testid="backend-row">
                <td className={backend.name}>{backend.name}</td>
                <td>
                  <span className="share">
                    <span className="track">
                      <i className={backend.name} style={{ width: `${backend.hit_share * 100}%` }} />
                    </span>
                    <span className="mono">{percent(backend.hit_share)}</span>
                  </span>
                </td>
                <td className="right mono">{percent(backend.solo_share)}</td>
                <td className="right mono">{ms(backend.p50_ms)}</td>
                <td className="right mono">{ms(backend.p95_ms)}</td>
                <td className={`right mono${backend.failures ? " warn" : " faint"}`}>
                  {backend.failures}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <div className="grid-2">
        <section className="card" data-testid="zero-hit-queries">
          <h2>Searches that found nothing</h2>
          {data.zero_hit_queries.length === 0 ? (
            <p className="muted">Every search in this window returned something.</p>
          ) : (
            <ul className="rows">
              {data.zero_hit_queries.map((item) => (
                <li key={item.query}>
                  <span className="ellipsis mono">“{item.query || "(empty)"}”</span>
                  <span className="mono">{item.count}×</span>
                  <span className="muted">{ago(item.last)}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
        <section className="card">
          <h2>Slowest searches</h2>
          {data.slowest.length === 0 ? (
            <p className="muted">No searches in this window.</p>
          ) : (
            <ul className="rows">
              {data.slowest.map((item) => (
                <li key={item.trace_id}>
                  <Link href={`/traces/${item.trace_id}`} className="ellipsis mono">
                    “{item.query || "(empty)"}”
                  </Link>
                  <span className="mono">{ms(item.duration_ms)}</span>
                  <span className="muted">{item.hits} hits</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
      <Capped show={data.truncated} />
    </div>
  );
}

export function ServiceView({ data }: { data: ServiceHealth }) {
  const jobs = data.jobs.by_status;
  const pending = jobs.pending ?? 0;
  const failed = jobs.failed ?? 0;
  return (
    <div data-testid="ops-service" className="ops-view">
      <div className="stats">
        <Stat id="requests" label="Requests" value={number.format(data.totals.requests)} />
        <Stat
          id="error-rate"
          label="Failed"
          value={percent(data.totals.error_rate)}
          note={`${data.totals.errors} requests`}
          tone={data.totals.error_rate > 0.05 ? "bad" : data.totals.errors ? "warn" : undefined}
        />
        <Stat
          id="queue"
          label="Jobs waiting"
          value={number.format(pending)}
          note={
            data.jobs.oldest_pending_seconds !== null
              ? `oldest ${Math.round(data.jobs.oldest_pending_seconds)}s`
              : undefined
          }
          tone={(data.jobs.oldest_pending_seconds ?? 0) > 300 ? "warn" : undefined}
        />
        <Stat
          id="failed-jobs"
          label="Jobs failed"
          value={number.format(failed)}
          tone={failed > 0 ? "bad" : undefined}
        />
        <Stat id="worker" label="Worker last ran" value={ago(data.worker_last_seen)} />
        <Stat
          id="dropped"
          label="Traces dropped"
          value={number.format(data.traces.dropped)}
          note={`${number.format(data.traces.stored)} stored`}
          tone={data.traces.dropped > 0 ? "warn" : undefined}
        />
      </div>
      <section className="card">
        <h2>Endpoints and jobs</h2>
        <table className="data">
          <thead>
            <tr>
              <th>Name</th>
              <th className="right">count</th>
              <th className="right">failed</th>
              <th className="right">degraded</th>
              <th className="right">median</th>
              <th className="right">95th</th>
            </tr>
          </thead>
          <tbody>
            {data.endpoints.map((endpoint) => (
              <tr key={`${endpoint.kind}-${endpoint.name}`} data-testid="endpoint-row">
                <td className="mono">
                  {endpoint.name} {endpoint.kind === "job" ? <span className="kind">job</span> : null}
                </td>
                <td className="right mono">{number.format(endpoint.count)}</td>
                <td className={`right mono${endpoint.errors ? " warn" : " faint"}`}>
                  {endpoint.errors}
                </td>
                <td className={`right mono${endpoint.degraded ? " warn" : " faint"}`}>
                  {endpoint.degraded}
                </td>
                <td className="right mono">{ms(endpoint.p50_ms)}</td>
                <td className="right mono">{ms(endpoint.p95_ms)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <div className="grid-2">
        <section className="card">
          <h2>Recent job failures</h2>
          {data.jobs.recent_failures.length === 0 ? (
            <p className="muted">No job has failed or retried.</p>
          ) : (
            <ul className="rows">
              {data.jobs.recent_failures.map((job) => (
                <li key={job.id}>
                  <span className="ellipsis mono">
                    {job.job_type} · {job.error ?? "unknown error"}
                  </span>
                  <span className={job.status === "failed" ? "warn" : "muted"}>
                    {job.status}, {job.attempts} tries
                  </span>
                  <span className="muted">{ago(job.at)}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
        <section className="card">
          <h2>Configuration</h2>
          <dl className="config">
            <dt>Embeddings</dt>
            <dd className="mono">
              {data.config.embedding_provider} · {data.config.embedding_model}
            </dd>
            <dt>Graph builder</dt>
            <dd className="mono">
              {data.config.classifier}
              {data.config.classification_model ? ` · ${data.config.classification_model}` : ""}
            </dd>
            <dt>Graph store</dt>
            <dd className="mono">{data.config.graph_store}</dd>
            <dt>Traces</dt>
            <dd className="mono">
              kept {data.traces.retention_days} days · sampling {percent(data.traces.sample_rate)}
            </dd>
            {data.database_bytes !== null ? (
              <>
                <dt>Database size</dt>
                <dd className="mono">{bytes(data.database_bytes)}</dd>
              </>
            ) : null}
          </dl>
        </section>
      </div>
      <Capped show={data.truncated} />
    </div>
  );
}
