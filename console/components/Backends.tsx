import type { TraceSummary } from "@/lib/types";

const ORDER = ["lexical", "vector", "graph"] as const;

export function backendsOf(trace: TraceSummary): string[] {
  const degraded = trace.summary.degraded ?? [];
  if (trace.kind === "job") return [];
  if (!trace.name.includes("/search") && !trace.name.includes("/context") && !trace.name.includes("/graph")) return [];
  return ORDER.filter((backend) => !degraded.some((reason) => reason.startsWith(backend)));
}

export function Backends({ trace }: { trace: TraceSummary }) {
  const active = backendsOf(trace);
  if (active.length === 0) return null;
  return (
    <span className="backends" aria-label={`backends: ${active.join(", ")}`}>
      {active.map((backend) => (
        <i key={backend} className={backend} title={backend} />
      ))}
    </span>
  );
}
