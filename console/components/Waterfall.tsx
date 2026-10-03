"use client";

import type { Span } from "@/lib/types";

export function Waterfall({
  spans,
  selected,
  onSelect,
}: {
  spans: Span[];
  selected: number;
  onSelect: (seq: number) => void;
}) {
  const total = Math.max(
    1,
    ...spans.map((span) => span.started_offset_ms + span.duration_ms),
  );
  return (
    <div className="waterfall" aria-label="Spans">
      {spans.map((span) => {
        const left = (span.started_offset_ms / total) * 100;
        const width = Math.max(0.6, (span.duration_ms / total) * 100);
        return (
          <button
            key={span.seq}
            type="button"
            data-testid="span-row"
            className={`span ${selected === span.seq ? "selected" : ""}`}
            onClick={() => onSelect(span.seq)}
            aria-pressed={selected === span.seq}
          >
            <span className="span-name">{span.name}</span>
            <span className="track">
              <span
                className={`bar ${span.name} ${span.status}`}
                style={{ left: `${left}%`, width: `${width}%` }}
              />
            </span>
            <span className="mono right">{span.duration_ms.toFixed(2)} ms</span>
          </button>
        );
      })}
    </div>
  );
}
