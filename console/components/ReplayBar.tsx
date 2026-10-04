"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { listTraces, relativeTime } from "@/lib/api";
import { eventFromTrace, type MapEvent } from "@/lib/map";

export type MapMode = "live" | "replay";
export type LiveState = "connecting" | "live" | "paused";

const WINDOWS = [
  { value: 3_600_000, label: "Last hour" },
  { value: 86_400_000, label: "Last day" },
];
const SPEEDS = [
  { value: 60, label: "1 min / s" },
  { value: 600, label: "10 min / s" },
  { value: 3600, label: "1 hour / s" },
];
const TICK_MS = 100;
const PAGE = 200;
const MAX_PAGES = 5;
const MAX_MARKS = 240;

interface Loaded {
  key: string;
  start: number;
  end: number;
  events: MapEvent[];
  capped: boolean;
}

async function loadEvents(organization: string, windowMs: number, key: string): Promise<Loaded> {
  const end = Date.now();
  const start = end - windowMs;
  const events: MapEvent[] = [];
  let cursor: string | undefined;
  let capped = false;
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const result = await listTraces({
      organization_id: organization,
      since: new Date(start).toISOString(),
      limit: PAGE,
      cursor,
    });
    for (const trace of result.items) {
      const event = eventFromTrace(trace);
      if (event) events.push(event);
    }
    if (!result.next_cursor) break;
    cursor = result.next_cursor;
    capped = page === MAX_PAGES - 1;
  }
  events.sort((a, b) => new Date(a.at).getTime() - new Date(b.at).getTime());
  return { key, start, end, events, capped };
}

export function ReplayBar({
  organization,
  mode,
  onMode,
  liveState,
  pulseCount,
  ticker,
  onEvent,
}: {
  organization: string;
  mode: MapMode;
  onMode: (mode: MapMode) => void;
  liveState: LiveState;
  pulseCount: number;
  ticker: MapEvent[];
  onEvent: (event: MapEvent) => void;
}) {
  const [windowMs, setWindowMs] = useState(WINDOWS[0].value);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playhead, setPlayhead] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(SPEEDS[1].value);
  const position = useRef(0);
  const emit = useRef(onEvent);
  useEffect(() => {
    emit.current = onEvent;
  }, [onEvent]);

  const key = `${organization}|${windowMs}`;
  useEffect(() => {
    if (mode !== "replay" || !organization) return;
    let cancelled = false;
    loadEvents(organization, windowMs, key)
      .then((result) => {
        if (cancelled) return;
        position.current = result.start;
        setLoaded(result);
        setPlayhead(result.start);
        setPlaying(false);
        setError(null);
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [mode, organization, windowMs, key]);

  const current = loaded?.key === key ? loaded : null;

  useEffect(() => {
    if (!playing || !current || mode !== "replay") return;
    const timer = setInterval(() => {
      const from = position.current;
      const to = Math.min(current.end, from + speed * TICK_MS);
      for (const event of current.events) {
        const at = new Date(event.at).getTime();
        if (at > from && at <= to) emit.current(event);
      }
      position.current = to;
      setPlayhead(to);
      if (to >= current.end) setPlaying(false);
    }, TICK_MS);
    return () => clearInterval(timer);
  }, [playing, speed, current, mode]);

  const marks = useMemo(() => {
    if (!current) return [];
    const span = current.end - current.start;
    const step = Math.max(1, Math.ceil(current.events.length / MAX_MARKS));
    return current.events
      .filter((_, index) => index % step === 0)
      .map((event) => ({
        key: event.traceId,
        kind: event.kind,
        left: ((new Date(event.at).getTime() - current.start) / span) * 100,
      }));
  }, [current]);

  const scrub = (value: number) => {
    if (!current) return;
    const at = current.start + (value / 1000) * (current.end - current.start);
    position.current = at;
    setPlayhead(at);
  };
  const togglePlay = () => {
    if (!current) return;
    if (!playing && position.current >= current.end) {
      position.current = current.start;
      setPlayhead(current.start);
    }
    setPlaying((p) => !p);
  };
  const state = mode === "replay" ? "replay" : liveState;

  return (
    <div className="replay-bar" data-mode={mode}>
      <div className="segmented" role="group" aria-label="Map time">
        <button type="button" aria-pressed={mode === "live"} onClick={() => {
            setPlaying(false);
            onMode("live");
          }}>
          Live
        </button>
        <button type="button" aria-pressed={mode === "replay"} onClick={() => onMode("replay")}>
          Replay
        </button>
      </div>
      <span className={`live-state ${state}`}>
        <i className={`live-dot${state === "live" ? "" : " off"}`} />
        <span data-testid="map-live-state">{state}</span>
      </span>
      {mode === "live" ? (
        <span className="mono muted">
          <span data-testid="map-pulse-count">{pulseCount}</span> events since you opened the map
        </span>
      ) : (
        <>
          <select
            aria-label="Replay window"
            value={windowMs}
            onChange={(e) => setWindowMs(Number(e.target.value))}
          >
            {WINDOWS.map((w) => (
              <option key={w.value} value={w.value}>
                {w.label}
              </option>
            ))}
          </select>
          <button type="button" className="play" onClick={togglePlay} disabled={!current}>
            {playing ? "Pause" : "Play"}
          </button>
          <select aria-label="Replay speed" value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>
            {SPEEDS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
          <div className="scrub">
            <div className="marks" aria-hidden="true">
              {marks.map((mark) => (
                <i key={mark.key} className={mark.kind} style={{ left: `${mark.left}%` }} />
              ))}
            </div>
            <input
              type="range"
              min={0}
              max={1000}
              aria-label="Replay position"
              disabled={!current}
              value={
                current ? Math.round(((playhead - current.start) / (current.end - current.start)) * 1000) : 0
              }
              onChange={(e) => scrub(Number(e.target.value))}
            />
          </div>
          <span className="mono muted when">
            {current ? new Date(playhead).toLocaleTimeString() : "loading…"}
          </span>
          <span className="mono muted">
            <span data-testid="replay-event-count">{current ? current.events.length : 0}</span> events
            {current?.capped ? " (newest only)" : ""}
          </span>
        </>
      )}
      {error ? <span className="search-error">Replay could not load: {error}</span> : null}
      <ol className="ticker" data-testid="map-ticker" aria-label="Latest events">
        {ticker.map((event) => (
          <li key={`${event.traceId}-${event.kind}`}>
            <Link href={`/traces/${event.traceId}`} title="Open this trace in replay">
              <i className={`swatch ${event.kind}`} />
              <span className="ellipsis">
                {event.kind === "recall" ? "recalled" : event.label} {event.ids.length}
                {event.kind === "recall" ? ` for ${event.label}` : ""}
              </span>
              <span className="mono muted">{relativeTime(event.at)}</span>
            </Link>
          </li>
        ))}
      </ol>
    </div>
  );
}
