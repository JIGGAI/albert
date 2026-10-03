import { ExplorerPanel } from "@/components/ExplorerPanel";

export default function ExplorerPage() {
  return (
    <>
      <div className="page-head">
        <h1>Explorer</h1>
        <span className="sub">Entities, memories and the edges between them, at any point in time.</span>
      </div>
      <ExplorerPanel />
    </>
  );
}
