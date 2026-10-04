import { BackendsPanel } from "@/components/BackendsPanel";

export default function BackendsPage() {
  return (
    <>
      <div className="page-head">
        <h1>Backends</h1>
        <span className="sub">
          Where the knowledge graph lives, what builds it, and whether both are working.
        </span>
      </div>
      <BackendsPanel />
    </>
  );
}
