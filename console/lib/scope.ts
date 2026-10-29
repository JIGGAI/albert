"use client";

import { useEffect, useState } from "react";
import { getScope, type WorkspaceParams } from "./api";
import type { ConsoleScope } from "./types";

/**
 * A workspace picker's value: "" for every workspace, a workspace id for that
 * workspace plus the organization-wide memories it shares, or SHARED for the
 * organization-wide memories alone.
 */
export const SHARED = "shared";
export const SHARED_LABEL = "Organization-wide (shared)";

export function workspaceParams(choice: string): WorkspaceParams {
  if (choice === SHARED) return { shared_only: "true" };
  return choice ? { workspace_id: choice } : {};
}

/** The workspace a trace-backed view filters by; traces have no "shared only". */
export function traceWorkspace(choice: string): string | undefined {
  return choice && choice !== SHARED ? choice : undefined;
}

let cached: Promise<ConsoleScope> | null = null;

/** The console key's scope, fetched once per page load; null while loading or on failure. */
export function useConsoleScope(): ConsoleScope | null {
  const [scope, setScope] = useState<ConsoleScope | null>(null);
  useEffect(() => {
    let cancelled = false;
    cached ??= getScope();
    cached
      .then((value) => {
        if (!cancelled) setScope(value);
      })
      .catch(() => {
        cached = null;
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return scope;
}

/** Whether the key is held to one workspace (admin never is). */
export function workspaceBound(scope: ConsoleScope | null): boolean {
  return !!scope && !scope.admin && scope.workspace !== null;
}

/** Whether the key can see more than one tenant, so a tenant picker changes anything. */
export function seesAllTenants(scope: ConsoleScope | null): boolean {
  return scope?.admin ?? false;
}

export function scopeLabel(scope: ConsoleScope): string {
  if (scope.admin) return "Every tenant (admin)";
  const tenant = scope.organization.name ?? "This tenant";
  return scope.workspace ? `${tenant} · ${scope.workspace.name} + shared` : tenant;
}
