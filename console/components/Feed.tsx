"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  listOrganizations,
  listTraces,
  listWorkspaces,
  openTraceStream,
  relativeTime,
  shortId,
} from "@/lib/api";
import { seesAllTenants, useConsoleScope } from "@/lib/scope";
import type { Organization, TraceSummary, Workspace } from "@/lib/types";
import { Backends } from "./Backends";
import { StatusPill } from "./StatusPill";
import { WorkspacePicker } from "./WorkspacePicker";

const MAX_ROWS = 500;

export function Feed() {
  const [traces, setTraces] = useState<TraceSummary[]>([]);
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [text, setText] = useState("");
  const [tenant, setTenant] = useState("");
  const [workspace, setWorkspace] = useState("");
  const scope = useConsoleScope();
  const [organizations, setOrganizations] = useState<Organization[]>([]);
  const [workspaces, setWorkspaces] = useState<(Workspace & { tenant: string })[]>([]);
  const [live, setLive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [fresh, setFresh] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    listOrganizations()
      .then(({ items }) => setOrganizations(items))
      .catch(() => setOrganizations([]));
  }, []);

  // Names for every workspace the feed can show, so rows read as names, not ids.
  useEffect(() => {
    let cancelled = false;
    Promise.all(
      organizations.map((o) =>
        listWorkspaces(o.id).then(({ items }) => items.map((w) => ({ ...w, tenant: o.id }))),
      ),
    )
      .then((lists) => {
        if (!cancelled) setWorkspaces(lists.flat());
      })
      .catch(() => {
        if (!cancelled) setWorkspaces([]);
      });
    return () => {
      cancelled = true;
    };
  }, [organizations]);

  const tenantNames = useMemo(() => new Map(organizations.map((o) => [o.id, o.name])), [organizations]);
  const workspaceNames = useMemo(() => new Map(workspaces.map((w) => [w.id, w.name])), [workspaces]);
  // Workspaces are picked within one tenant: the chosen one, or the only one the key sees.
  const workspaceTenant = tenant || (scope && !seesAllTenants(scope) ? scope.organization.id : "");
  const tenantWorkspaces = useMemo(
    () => workspaces.filter((w) => w.tenant === workspaceTenant),
    [workspaces, workspaceTenant],
  );

  useEffect(() => {
    let cancelled = false;
    listTraces({
      limit: 100,
      kind: kind || undefined,
      status: status || undefined,
      organization_id: tenant || undefined,
      workspace_id: workspace || undefined,
    })
      .then((page) => {
        if (!cancelled) setTraces(page.items);
      })
      .catch((e: Error) => setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [kind, status, tenant, workspace]);

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
      if (tenant && trace.organization_id !== tenant) return false;
      if (workspace && trace.workspace_id !== workspace) return false;
      if (!needle) return true;
      const query = String(trace.summary.query ?? "").toLowerCase();
      return trace.name.toLowerCase().includes(needle) || query.includes(needle);
    });
  }, [traces, kind, status, tenant, workspace, text]);

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
        {seesAllTenants(scope) || organizations.length > 1 ? (
          <select
            aria-label="Tenant"
            value={tenant}
            onChange={(e) => {
              setTenant(e.target.value);
              setWorkspace("");
            }}
          >
            <option value="">All tenants</option>
            {organizations.map((o) => (
              <option key={o.id} value={o.id}>
                {o.name}
              </option>
            ))}
          </select>
        ) : null}
        {workspaceTenant ? (
          <WorkspacePicker
            scope={scope}
            workspaces={tenantWorkspaces}
            value={workspace}
            onChange={setWorkspace}
            allowShared={false}
          />
        ) : null}
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
          <span>Scope</span>
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
            <span className="ellipsis muted" title={trace.workspace_id ?? "organization-wide"}>
              {traceScope(trace, tenantNames, workspaceNames)}
            </span>
            <span className="right muted">{relativeTime(trace.started_at, now)}</span>
          </Link>
        ))}
      </div>
    </>
  );
}

/** "Tenant · workspace" for a row; organization-wide work has no workspace. */
function traceScope(
  trace: TraceSummary,
  tenants: Map<string, string>,
  workspaces: Map<string, string>,
): string {
  const tenant = trace.organization_id
    ? (tenants.get(trace.organization_id) ?? shortId(trace.organization_id))
    : "none";
  const workspace = trace.workspace_id
    ? (workspaces.get(trace.workspace_id) ?? shortId(trace.workspace_id))
    : "organization-wide";
  return `${tenant} · ${workspace}`;
}
