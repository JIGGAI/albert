import { Suspense } from "react";
import { MapPanel } from "@/components/MapPanel";

export default function MapPage() {
  return (
    <>
      <div className="page-head">
        <h1>Memory map</h1>
        <span className="sub">
          Every memory, what it connects to, and what agents are recalling right now.
        </span>
      </div>
      <Suspense fallback={<p className="muted">Loading map…</p>}>
        <MapPanel />
      </Suspense>
    </>
  );
}
