import { WorkspacesPanel } from "@/components/WorkspacesPanel";

export default function WorkspacesPage() {
  return (
    <>
      <div className="page-head">
        <h1>Workspaces</h1>
        <span className="sub">Keep each team&apos;s or agent group&apos;s memories apart.</span>
      </div>
      <WorkspacesPanel />
    </>
  );
}
