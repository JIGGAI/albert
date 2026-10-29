"use client";

import { Fragment, useCallback, useEffect, useState } from "react";
import {
  createApiKey,
  createConnectionLink,
  createWorkspace,
  deleteWorkspace,
  getCredentials,
  listSettingsWorkspaces,
  renameWorkspace,
  revokeApiKey,
  revokeConnectionLink,
} from "@/lib/api";
import { SHARED_LABEL, useConsoleScope } from "@/lib/scope";
import type {
  Credentials,
  MemoryOutcome,
  ScopeFields,
  SettingsWorkspace,
  SettingsWorkspaces,
} from "@/lib/types";

const CAPABILITY_HELP: Record<string, string> = {
  "console.read": "Read-only console view. All Albert Cloud needs.",
  "memory.read": "Search and read memories",
  "memory.write": "Store memories",
  "memory.write.organization":
    "Also write and change the organization-wide memories every workspace shares",
  "memory.delete": "Delete memories",
  "memory.export": "Export everything in scope",
  "memory.import": "Import a bundle",
  "memory.confidential": "Also see confidential memories",
  "memory.restricted": "Also see restricted memories",
  "graph.query": "Query the knowledge graph",
  "graph.write": "Add relationships",
  "working_memory.read": "Read working memory",
  "working_memory.write": "Write working memory",
  "locks.acquire": "Take coordination locks",
  "capture.write": "Record automatic-capture sessions and events",
  "capture.system": "Also record privileged system and tool events, and edit capture profiles",
};

const EXPIRY_OPTIONS = [
  { label: "15 minutes", seconds: 15 * 60 },
  { label: "1 hour", seconds: 3600 },
  { label: "24 hours", seconds: 24 * 3600 },
  { label: "7 days", seconds: 7 * 24 * 3600 },
];

const when = new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeStyle: "short" });

function at(value: string | null): string {
  return value ? when.format(new Date(value)) : "–";
}

function message(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}

function CapabilityPicker({
  options,
  value,
  onChange,
}: {
  options: string[];
  value: string[];
  onChange: (next: string[]) => void;
}) {
  return (
    <fieldset className="capabilities">
      <legend>Capabilities</legend>
      {options.map((capability) => (
        <label key={capability} className="check">
          <input
            type="checkbox"
            checked={value.includes(capability)}
            onChange={(event) =>
              onChange(
                event.target.checked
                  ? [...value, capability]
                  : value.filter((item) => item !== capability),
              )
            }
          />
          <code>{capability}</code>
          <span className="muted">{CAPABILITY_HELP[capability] ?? ""}</span>
        </label>
      ))}
    </fieldset>
  );
}

/** Where a credential is held: one workspace (plus the shared memories), or the whole organization. */
function scopeName(row: ScopeFields): string {
  return row.workspace_name ?? (row.workspace_id ? "workspace" : "Organization-wide");
}

/**
 * Which workspace a new credential is held to. An organization-wide console
 * can issue into any workspace or the whole organization; a workspace-scoped
 * one only into its own, so there is nothing to pick.
 */
function ScopePicker({
  issuer,
  workspaces,
  value,
  onChange,
}: {
  issuer: ScopeFields;
  workspaces: SettingsWorkspaces | null;
  value: string;
  onChange: (value: string) => void;
}) {
  if (issuer.workspace_id) {
    return (
      <p className="field">
        <span>Scope</span>
        <strong data-testid="issue-scope">
          {issuer.workspace_name ?? "This workspace"}, plus the shared memories
        </strong>
      </p>
    );
  }
  return (
    <label className="field">
      <span>Scope</span>
      <select value={value} onChange={(e) => onChange(e.target.value)} aria-label="Scope">
        <option value="">Organization-wide: every workspace</option>
        {(workspaces?.items ?? []).map((w) => (
          <option key={w.id} value={w.id}>
            {`${w.name} + shared`}
          </option>
        ))}
      </select>
    </label>
  );
}

function Secret({ label, value, note }: { label: string; value: string; note: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="secret" role="status">
      <span className="label">{label}</span>
      <div className="secret-row">
        <code data-testid="secret">{value}</code>
        <button
          type="button"
          onClick={() => {
            navigator.clipboard?.writeText(value).then(() => setCopied(true));
          }}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <p className="notice">{note}</p>
    </div>
  );
}

function LinkForm({
  options,
  issuer,
  workspaces,
  onCreated,
}: {
  options: string[];
  issuer: ScopeFields;
  workspaces: SettingsWorkspaces | null;
  onCreated: () => void;
}) {
  const [name, setName] = useState("Albert Cloud");
  const [workspace, setWorkspace] = useState("");
  const [capabilities, setCapabilities] = useState<string[]>(["console.read"]);
  const [expires, setExpires] = useState(3600);
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await createConnectionLink({
        name,
        capabilities,
        expires_in_seconds: expires,
        workspace_id: workspace || undefined,
      });
      setUrl(created.url);
      onCreated();
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card settings-card" aria-labelledby="link-title">
      <h2 id="link-title">One-time connection link</h2>
      <p className="muted">
        Recommended. Paste it into Albert Cloud&apos;s <b>Connect external Albert</b>, or give it to
        an agent. It works once, then mints a key with only the capabilities you choose here.
      </p>
      <form onSubmit={submit}>
        <label className="field">
          <span>Name</span>
          <input value={name} maxLength={200} required onChange={(e) => setName(e.target.value)} />
        </label>
        <ScopePicker issuer={issuer} workspaces={workspaces} value={workspace} onChange={setWorkspace} />
        <CapabilityPicker options={options} value={capabilities} onChange={setCapabilities} />
        <label className="field">
          <span>Expires after</span>
          <select value={expires} onChange={(e) => setExpires(Number(e.target.value))}>
            {EXPIRY_OPTIONS.map((option) => (
              <option key={option.seconds} value={option.seconds}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" disabled={busy || !capabilities.length || !name.trim()}>
          Create link
        </button>
      </form>
      {error ? <p className="search-error" role="alert">{error}</p> : null}
      {url ? (
        <Secret
          label="Connection link"
          value={url}
          note="Shown once. Anyone with this link can redeem it until it is used or expires."
        />
      ) : null}
    </section>
  );
}

function KeyForm({
  options,
  issuer,
  workspaces,
  restUrl,
  mcpUrl,
  onCreated,
}: {
  options: string[];
  issuer: ScopeFields;
  workspaces: SettingsWorkspaces | null;
  restUrl: string;
  mcpUrl: string;
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [capabilities, setCapabilities] = useState<string[]>(["memory.read"]);
  const [key, setKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const created = await createApiKey({
        name,
        capabilities,
        workspace_id: workspace || undefined,
      });
      setKey(created.credential);
      onCreated();
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card settings-card" aria-labelledby="key-title">
      <h2 id="key-title">Least-privilege API key</h2>
      <p className="muted">
        For manual setup. Each key gets its own identity, so you can audit and revoke one agent
        without touching the others.
      </p>
      <form onSubmit={submit}>
        <label className="field">
          <span>Name</span>
          <input
            value={name}
            maxLength={200}
            required
            placeholder="Research agent"
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <ScopePicker issuer={issuer} workspaces={workspaces} value={workspace} onChange={setWorkspace} />
        <CapabilityPicker options={options} value={capabilities} onChange={setCapabilities} />
        <button type="submit" disabled={busy || !capabilities.length || !name.trim()}>
          Create key
        </button>
      </form>
      {error ? <p className="search-error" role="alert">{error}</p> : null}
      {key ? (
        <>
          <Secret
            label="API key"
            value={key}
            note="Shown once. Store it in the agent's secret store; Albert keeps only a hash."
          />
          <dl className="config">
            <dt>REST URL</dt>
            <dd>
              <code>{restUrl}</code>
            </dd>
            <dt>MCP URL</dt>
            <dd>
              <code>{mcpUrl}</code>
            </dd>
          </dl>
        </>
      ) : null}
    </section>
  );
}

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

function DeleteWorkspace({
  workspace,
  others,
  canForget,
  onCancel,
  onDeleted,
}: {
  workspace: SettingsWorkspace;
  others: SettingsWorkspace[];
  canForget: boolean;
  onCancel: () => void;
  onDeleted: () => void;
}) {
  const [outcome, setOutcome] = useState<MemoryOutcome>("organization");
  const [to, setTo] = useState(others[0]?.id ?? "");
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ready =
    !busy &&
    (outcome !== "workspace" || Boolean(to)) &&
    (outcome !== "delete" || typed === workspace.name);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await deleteWorkspace(workspace.id, outcome, outcome === "workspace" ? to : undefined);
      onDeleted();
    } catch (cause) {
      setError(message(cause));
      setBusy(false);
    }
  }

  const choices: { value: MemoryOutcome; label: string; help: string; disabled?: string }[] = [
    {
      value: "organization",
      label: "Make them organization-wide",
      help: "Every workspace will read them.",
    },
    {
      value: "workspace",
      label: "Move them to another workspace",
      help: "Entities already there are merged, not duplicated.",
      disabled: others.length ? undefined : "There is no other workspace to move them to.",
    },
    {
      value: "delete",
      label: "Delete them",
      help: "Forgotten for good, with their graph.",
      disabled: canForget ? undefined : "This console's key does not have memory.delete.",
    },
  ];

  return (
    <form onSubmit={submit} className="delete-workspace" data-testid="delete-workspace">
      <fieldset>
        <legend>
          Delete <b>{workspace.name}</b>. What happens to its{" "}
          {plural(workspace.memories, "memory", "memories")}?
        </legend>
        {choices.map((choice) => (
          <label key={choice.value} className="check">
            <input
              type="radio"
              name="memories"
              value={choice.value}
              checked={outcome === choice.value}
              disabled={Boolean(choice.disabled)}
              onChange={() => setOutcome(choice.value)}
            />
            <span>{choice.label}</span>
            <span className="muted">{choice.disabled ?? choice.help}</span>
          </label>
        ))}
      </fieldset>
      {outcome === "workspace" ? (
        <label>
          Move to{" "}
          <select aria-label="Move to" value={to} onChange={(e) => setTo(e.target.value)}>
            {others.map((other) => (
              <option key={other.id} value={other.id}>
                {other.name}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      {outcome === "delete" ? (
        <label>
          Type <b>{workspace.name}</b> to confirm{" "}
          <input
            aria-label="Type the workspace name to confirm"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
          />
        </label>
      ) : null}
      <p className="muted" data-testid="delete-workspace-revokes">
        {workspace.credentials
          ? `${plural(workspace.credentials, "API key or connection link", "API keys and connection links")} for this workspace stop working.`
          : "No API keys or connection links use this workspace."}
      </p>
      <div className="inline-form">
        <button type="submit" className="danger" disabled={!ready}>
          {busy ? "Deleting…" : "Delete workspace"}
        </button>
        <button type="button" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
      {error ? <p className="search-error" role="alert">{error}</p> : null}
    </form>
  );
}

function WorkspacesCard({
  data,
  onChanged,
}: {
  data: SettingsWorkspaces;
  onChanged: () => void;
}) {
  const [name, setName] = useState("");
  const [editing, setEditing] = useState<{ id: string; name: string } | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const scope = useConsoleScope();
  const canForget = Boolean(scope?.admin || scope?.capabilities.includes("memory.delete"));

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      await createWorkspace(name.trim());
      setName("");
      onChanged();
    } catch (cause) {
      setError(message(cause));
    }
  }

  async function rename(event: React.FormEvent) {
    event.preventDefault();
    if (!editing) return;
    setError(null);
    try {
      await renameWorkspace(editing.id, editing.name.trim());
      setEditing(null);
      onChanged();
    } catch (cause) {
      setError(message(cause));
    }
  }

  return (
    <section className="card" aria-labelledby="workspaces-title">
      <h2 id="workspaces-title">Workspaces</h2>
      <p className="muted">
        A workspace keeps one team&apos;s or agent group&apos;s memories apart. Every workspace also
        reads the organization-wide memories (<b>{data.shared_memories}</b> now); a workspace key
        changes those only with <code>memory.write.organization</code>.
      </p>
      <table className="data" data-testid="workspaces">
        <thead>
          <tr>
            <th>Name</th>
            <th className="right">Memories</th>
            <th>Created</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {data.items.map((workspace) => (
            <Fragment key={workspace.id}>
              <tr>
                <td>
                  {editing?.id === workspace.id ? (
                    <form onSubmit={rename} className="inline-form">
                      <input
                        aria-label="Workspace name"
                        value={editing.name}
                        maxLength={200}
                        required
                        onChange={(e) => setEditing({ id: workspace.id, name: e.target.value })}
                      />
                      <button type="submit" disabled={!editing.name.trim()}>
                        Save
                      </button>
                      <button type="button" onClick={() => setEditing(null)}>
                        Cancel
                      </button>
                    </form>
                  ) : (
                    workspace.name
                  )}
                </td>
                <td className="right mono">{workspace.memories}</td>
                <td>{at(workspace.created_at)}</td>
                <td className="right">
                  {data.can_manage && editing?.id !== workspace.id && deleting !== workspace.id ? (
                    <span className="row-actions">
                      <button
                        type="button"
                        onClick={() => setEditing({ id: workspace.id, name: workspace.name })}
                      >
                        Rename
                      </button>
                      <button type="button" onClick={() => setDeleting(workspace.id)}>
                        Delete
                      </button>
                    </span>
                  ) : null}
                </td>
              </tr>
              {deleting === workspace.id ? (
                <tr>
                  <td colSpan={4}>
                    <DeleteWorkspace
                      workspace={workspace}
                      others={data.items.filter((other) => other.id !== workspace.id)}
                      canForget={canForget}
                      onCancel={() => setDeleting(null)}
                      onDeleted={() => {
                        setDeleting(null);
                        onChanged();
                      }}
                    />
                  </td>
                </tr>
              ) : null}
            </Fragment>
          ))}
          <tr>
            <td className="muted">{SHARED_LABEL}</td>
            <td className="right mono">{data.shared_memories}</td>
            <td className="muted">read by every workspace</td>
            <td />
          </tr>
        </tbody>
      </table>
      {data.can_manage ? (
        <form onSubmit={create} className="inline-form">
          <input
            aria-label="New workspace name"
            placeholder="New workspace name"
            value={name}
            maxLength={200}
            onChange={(e) => setName(e.target.value)}
          />
          <button type="submit" disabled={!name.trim()}>
            Create workspace
          </button>
        </form>
      ) : (
        <p className="muted">
          Only an organization-wide key can create, rename or delete workspaces.
        </p>
      )}
      {error ? <p className="search-error" role="alert">{error}</p> : null}
    </section>
  );
}

export function SettingsPanel() {
  const [data, setData] = useState<Credentials | null>(null);
  const [workspaces, setWorkspaces] = useState<SettingsWorkspaces | null>(null);
  const [error, setError] = useState<{ status?: number; text: string } | null>(null);

  const load = useCallback(() => {
    getCredentials()
      .then((next) => {
        setData(next);
        setError(null);
      })
      .catch((cause) => setError({ status: cause?.status, text: message(cause) }));
    listSettingsWorkspaces()
      .then(setWorkspaces)
      .catch(() => setWorkspaces(null));
  }, []);

  useEffect(load, [load]);

  async function revoke(kind: "key" | "link", id: string, name: string) {
    if (!window.confirm(`Revoke ${kind === "key" ? "API key" : "connection link"} "${name}"?`)) {
      return;
    }
    try {
      await (kind === "key" ? revokeApiKey(id) : revokeConnectionLink(id));
      load();
    } catch (cause) {
      window.alert(message(cause));
    }
  }

  if (error?.status === 403) {
    return (
      <div className="error-box" role="alert">
        <p>
          The console&apos;s API key can&apos;t manage credentials. Issue it a key with{" "}
          <code>keys.manage</code> and restart the console. Settings issues credentials no wider
          than the console principal: give it no workspace to manage the whole organization.
        </p>
        <pre className="mono">
          albert-admin create-key --principal-id &lt;console principal id&gt; --name console
          --capabilities console.read,memory.read,keys.manage
        </pre>
      </div>
    );
  }
  if (error) return <div className="error-box" role="alert">{error.text}</div>;
  if (!data) return <p className="empty">Loading…</p>;

  const options = data.grantable_capabilities;
  return (
    <div className="ops settings">
      <div className="grid-2">
        <LinkForm options={options} issuer={data} workspaces={workspaces} onCreated={load} />
        <KeyForm
          options={options}
          issuer={data}
          workspaces={workspaces}
          restUrl={data.rest_url}
          mcpUrl={data.mcp_url}
          onCreated={load}
        />
      </div>

      {workspaces ? <WorkspacesCard data={workspaces} onChanged={load} /> : null}

      <section className="card" aria-labelledby="links-title">
        <h2 id="links-title">Connection links</h2>
        {data.connection_links.length ? (
          <table className="data" data-testid="links">
            <thead>
              <tr>
                <th>Name</th>
                <th>Scope</th>
                <th>Capabilities</th>
                <th>State</th>
                <th>Expires</th>
                <th>Used</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.connection_links.map((link) => (
                <tr key={link.id}>
                  <td>{link.name}</td>
                  <td>{scopeName(link)}</td>
                  <td className="mono">{link.capabilities.join(", ")}</td>
                  <td>
                    <span className={`badge ${link.state === "open" ? "on" : ""}`}>{link.state}</span>
                  </td>
                  <td>{at(link.expires_at)}</td>
                  <td>{at(link.used_at)}</td>
                  <td className="right">
                    {link.state === "open" ? (
                      <button type="button" className="revoke" onClick={() => revoke("link", link.id, link.name)}>
                        Revoke
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">No connection links yet.</p>
        )}
      </section>

      <section className="card" aria-labelledby="keys-title">
        <h2 id="keys-title">API keys</h2>
        <table className="data" data-testid="keys">
          <thead>
            <tr>
              <th>Name</th>
              <th>Scope</th>
              <th>Prefix</th>
              <th>Capabilities</th>
              <th>Created</th>
              <th>Last used</th>
              <th>State</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.api_keys.map((key) => (
              <tr key={key.id}>
                <td>
                  {key.name}
                  {key.current ? <span className="muted"> (this console)</span> : null}
                </td>
                <td>{scopeName(key)}</td>
                <td className="mono">{key.prefix}</td>
                <td className="mono">{key.capabilities.join(", ")}</td>
                <td>{at(key.created_at)}</td>
                <td>{at(key.last_used_at)}</td>
                <td>
                  <span className={`badge ${key.active ? "on" : ""}`}>{key.active ? "active" : "revoked"}</span>
                </td>
                <td className="right">
                  {key.active && !key.current ? (
                    <button type="button" className="revoke" onClick={() => revoke("key", key.id, key.name)}>
                      Revoke
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
