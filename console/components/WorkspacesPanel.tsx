"use client";

import { Fragment, useCallback, useEffect, useState } from "react";
import { at, message } from "@/components/SettingsPanel";
import { createWorkspace, deleteWorkspace, listSettingsWorkspaces, renameWorkspace } from "@/lib/api";
import { SHARED_LABEL, useConsoleScope } from "@/lib/scope";
import type { MemoryOutcome, SettingsWorkspace, SettingsWorkspaces } from "@/lib/types";

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

export function WorkspacesPanel() {
  const [data, setData] = useState<SettingsWorkspaces | null>(null);
  const [error, setError] = useState<{ status?: number; text: string } | null>(null);

  const load = useCallback(() => {
    listSettingsWorkspaces()
      .then((next) => {
        setData(next);
        setError(null);
      })
      .catch((cause) => setError({ status: cause?.status, text: message(cause) }));
  }, []);

  useEffect(load, [load]);

  if (error?.status === 403) {
    return (
      <div className="error-box" role="alert">
        <p>
          The console&apos;s API key can&apos;t manage workspaces. Issue it a key with{" "}
          <code>keys.manage</code> and no workspace, and restart the console.
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
  return (
    <div className="ops settings">
      <WorkspacesCard data={data} onChanged={load} />
    </div>
  );
}
