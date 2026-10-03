"use client";

import { useEffect, useState } from "react";

export function TimeSlider({
  min,
  max,
  value,
  onChange,
}: {
  min: number;
  max: number;
  value: number;
  onChange: (value: number) => void;
}) {
  // Derived state: when the committed value changes from outside, adopt it.
  const [state, setState] = useState({ draft: value, synced: value });
  if (state.synced !== value) {
    setState({ draft: value, synced: value });
  }
  const draft = state.synced === value ? state.draft : value;

  // Debounce so a drag does not fetch (and cache) a snapshot per pixel.
  useEffect(() => {
    if (draft === value) return;
    const timer = setTimeout(() => onChange(draft), 300);
    return () => clearTimeout(timer);
  }, [draft, value, onChange]);

  const label = draft >= max ? "now" : new Date(draft).toLocaleString();
  return (
    <label className="time-slider">
      <span className="muted">As of</span>
      <input
        data-testid="time-slider"
        type="range"
        min={0}
        max={1000}
        value={max > min ? Math.round(((draft - min) / (max - min)) * 1000) : 1000}
        onChange={(e) =>
          setState((s) => ({ ...s, draft: min + (Number(e.target.value) / 1000) * (max - min) }))
        }
        aria-label="Point in time for edge validity"
      />
      <span className="mono">{label}</span>
    </label>
  );
}
