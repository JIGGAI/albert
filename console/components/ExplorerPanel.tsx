"use client";

import type { TraceDetail } from "@/lib/types";

// Task 10 replaces this with the 3D explorer.
export function ExplorerPanel({ trace }: { trace?: TraceDetail }) {
  return (
    <div data-testid="explorer-slot" className="muted">
      {trace ? "Graph view arrives in the next step." : null}
    </div>
  );
}
