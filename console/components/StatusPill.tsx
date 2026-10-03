import type { TraceStatus } from "@/lib/types";

export function StatusPill({ status, code }: { status: TraceStatus; code?: number | null }) {
  return (
    <span className={`pill ${status}`}>
      {status}
      {code ? <span className="mono">{code}</span> : null}
    </span>
  );
}
