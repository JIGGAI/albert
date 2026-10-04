"use client";

import { COLOR_BY, LINK_KINDS, distinct, type ColorBy, type Filters } from "@/lib/map";
import type { MapNode, Organization, Workspace } from "@/lib/types";

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

function Choice({
  label,
  all,
  value,
  options,
  onChange,
}: {
  label: string;
  all: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  if (options.length < 2 && !value) return null;
  return (
    <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">{all}</option>
      {options.map((option) => (
        <option key={option} value={option}>
          {option}
        </option>
      ))}
    </select>
  );
}

export function MapToolbar({
  organizations,
  organization,
  onOrganization,
  workspaces,
  workspace,
  onWorkspace,
  nodes,
  filters,
  onFilters,
  colorBy,
  onColorBy,
  compact,
  children,
}: {
  organizations: Organization[];
  organization: string;
  onOrganization: (id: string) => void;
  workspaces: Workspace[];
  workspace: string;
  onWorkspace: (id: string) => void;
  nodes: MapNode[];
  filters: Filters;
  onFilters: (filters: Filters) => void;
  colorBy: ColorBy;
  onColorBy: (value: ColorBy) => void;
  /** The trace-replay side panel shows the pickers only. */
  compact: boolean;
  children?: React.ReactNode;
}) {
  const known = organizations.some((o) => o.id === organization);
  return (
    <div className="toolbar map-toolbar">
      <select aria-label="Tenant" value={organization} onChange={(e) => onOrganization(e.target.value)}>
        {organization && !known ? <option value={organization}>selected tenant</option> : null}
        {organizations.map((o) => (
          <option key={o.id} value={o.id}>
            {`${o.name} · ${plural(o.memories, "memory", "memories")}`}
          </option>
        ))}
        {organizations.length === 0 && !organization ? <option value="">no tenants yet</option> : null}
      </select>
      <select aria-label="Workspace" value={workspace} onChange={(e) => onWorkspace(e.target.value)}>
        <option value="">All workspaces</option>
        {workspaces.map((w) => (
          <option key={w.id} value={w.id}>
            {`${w.name} · ${w.memories}`}
          </option>
        ))}
      </select>
      {compact ? null : (
        <>
          {children}
          <Choice
            label="Team"
            all="All teams"
            value={filters.team}
            options={distinct(nodes, "team")}
            onChange={(team) => onFilters({ ...filters, team })}
          />
          <Choice
            label="Role"
            all="All roles"
            value={filters.role}
            options={distinct(nodes, "role")}
            onChange={(role) => onFilters({ ...filters, role })}
          />
          <Choice
            label="Type"
            all="All types"
            value={filters.type}
            options={distinct(nodes, "type")}
            onChange={(type) => onFilters({ ...filters, type })}
          />
          <label className="check">
            <input
              type="checkbox"
              checked={filters.recalledOnly}
              onChange={(e) => onFilters({ ...filters, recalledOnly: e.target.checked })}
            />
            Recalled only
          </label>
          <span className="spacer" />
          <label className="inline">
            <span className="muted">Color by</span>
            <select
              aria-label="Color by"
              value={colorBy}
              onChange={(e) => onColorBy(e.target.value as ColorBy)}
            >
              {COLOR_BY.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </>
      )}
    </div>
  );
}

export function LinkToggles({
  filters,
  onFilters,
}: {
  filters: Filters;
  onFilters: (filters: Filters) => void;
}) {
  return (
    <fieldset className="link-toggles">
      <legend className="muted">Links</legend>
      {LINK_KINDS.map(({ kind, label, hint }) => (
        <label key={kind} className="check" title={hint}>
          <input
            type="checkbox"
            checked={filters.kinds[kind]}
            onChange={(e) =>
              onFilters({ ...filters, kinds: { ...filters.kinds, [kind]: e.target.checked } })
            }
          />
          <i className={`link-key ${kind}`} aria-hidden="true" />
          {label}
        </label>
      ))}
    </fieldset>
  );
}
