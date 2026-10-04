import { Suspense } from "react";
import { OpsPanel } from "@/components/OpsPanel";

export default function OpsPage() {
  return (
    <>
      <div className="page-head">
        <h1>Operations</h1>
        <span className="sub">Who is using memory, what it holds, and how well it is working.</span>
      </div>
      <Suspense fallback={<p className="muted">Loading…</p>}>
        <OpsPanel />
      </Suspense>
    </>
  );
}
