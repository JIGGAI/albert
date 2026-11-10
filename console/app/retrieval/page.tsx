import { Suspense } from "react";
import { OpsPanel } from "@/components/OpsPanel";
import { VIEWS } from "@/lib/ops-views";

const view = VIEWS.find((v) => v.id === "retrieval")!;

export default function RetrievalPage() {
  return (
    <>
      <div className="page-head">
        <h1>{view.label}</h1>
        <span className="sub">{view.sub}</span>
      </div>
      <Suspense fallback={<p className="muted">Loading…</p>}>
        <OpsPanel view="retrieval" />
      </Suspense>
    </>
  );
}
