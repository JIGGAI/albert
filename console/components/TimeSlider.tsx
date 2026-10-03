"use client";

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
  const label = value >= max ? "now" : new Date(value).toLocaleString();
  return (
    <label className="time-slider">
      <span className="muted">As of</span>
      <input
        data-testid="time-slider"
        type="range"
        min={0}
        max={1000}
        value={max > min ? Math.round(((value - min) / (max - min)) * 1000) : 1000}
        onChange={(e) => onChange(min + (Number(e.target.value) / 1000) * (max - min))}
        aria-label="Point in time for edge validity"
      />
      <span className="mono">{label}</span>
    </label>
  );
}
