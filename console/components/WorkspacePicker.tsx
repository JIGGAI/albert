"use client";

import { SHARED, SHARED_LABEL, workspaceBound } from "@/lib/scope";
import type { ConsoleScope, Workspace } from "@/lib/types";

/**
 * Pick which workspace a view covers. A workspace includes the
 * organization-wide memories it shares; "Organization-wide (shared)" shows
 * those alone. A workspace-scoped console key only ever sees its own
 * workspace, so for it "" means that workspace and nothing else is offered.
 */
export function WorkspacePicker({
  scope,
  workspaces,
  value,
  onChange,
  shared = 0,
  allowShared = true,
}: {
  scope: ConsoleScope | null;
  workspaces: Workspace[];
  value: string;
  onChange: (value: string) => void;
  /** How many organization-wide memories there are, for the option's count. */
  shared?: number;
  /** Trace-backed views have no "shared only": traces belong to one workspace or none. */
  allowShared?: boolean;
}) {
  const plus = allowShared ? " + shared" : "";
  return (
    <select aria-label="Workspace" value={value} onChange={(e) => onChange(e.target.value)}>
      {workspaceBound(scope) ? (
        <option value="">{`${scope?.workspace?.name ?? "This workspace"}${plus}`}</option>
      ) : (
        <>
          <option value="">All workspaces</option>
          {workspaces.map((w) => (
            <option key={w.id} value={w.id}>
              {`${w.name} · ${w.memories}${plus}`}
            </option>
          ))}
        </>
      )}
      {allowShared ? <option value={SHARED}>{`${SHARED_LABEL} · ${shared}`}</option> : null}
    </select>
  );
}
