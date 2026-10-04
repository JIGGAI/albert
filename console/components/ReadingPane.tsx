"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { getMemory, relativeTime } from "@/lib/api";
import type { MemoryDetail, RelatedMemory } from "@/lib/types";

const MIN_WIDTH = 320;
const MAX_WIDTH = 820;
const KIND_LABEL: Record<RelatedMemory["kind"], string> = {
  sequence: "In sequence",
  similar: "Similar",
  recalled: "Recalled together",
};

function strength(item: RelatedMemory): string {
  if (item.kind === "similar") return `${Math.round(item.weight * 100)}% alike`;
  if (item.kind === "recalled") return `${Math.round(item.weight)}×`;
  return item.direction ?? "";
}

export function ReadingPane({
  memoryId,
  onNavigate,
  onClose,
  onLoaded,
}: {
  memoryId: string;
  onNavigate: (id: string) => void;
  onClose: () => void;
  onLoaded?: (memory: MemoryDetail) => void;
}) {
  const [loadedMemory, setLoadedMemory] = useState<MemoryDetail | null>(null);
  const [failure, setFailure] = useState<{ id: string; message: string } | null>(null);
  const [width, setWidth] = useState(460);
  const body = useRef<HTMLDivElement>(null);
  const loaded = useRef(onLoaded);
  useEffect(() => {
    loaded.current = onLoaded;
  }, [onLoaded]);

  useEffect(() => {
    let cancelled = false;
    getMemory(memoryId)
      .then((detail) => {
        if (cancelled) return;
        setLoadedMemory(detail);
        body.current?.scrollTo({ top: 0 });
        loaded.current?.(detail);
      })
      .catch((e: Error) => {
        if (!cancelled) setFailure({ id: memoryId, message: e.message });
      });
    return () => {
      cancelled = true;
    };
  }, [memoryId]);

  // While the next memory loads, the previous one stays readable instead of flashing empty.
  const memory = loadedMemory;
  const stale = memory !== null && memory.id !== memoryId;
  const error = failure?.id === memoryId ? failure.message : null;

  const startResize = (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = width;
    const move = (e: PointerEvent) =>
      setWidth(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, startWidth + (startX - e.clientX))));
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
  };
  const resizeByKey = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowLeft") setWidth((w) => Math.min(MAX_WIDTH, w + 24));
    if (event.key === "ArrowRight") setWidth((w) => Math.max(MIN_WIDTH, w - 24));
  };

  const groups = (["sequence", "similar", "recalled"] as const)
    .map((kind) => ({ kind, items: memory?.related.filter((r) => r.kind === kind) ?? [] }))
    .filter((group) => group.items.length > 0);

  return (
    <aside
      className="reading-pane"
      data-testid="reading-pane"
      style={{ width }}
      aria-label="Memory"
      aria-busy={stale || (!memory && !error)}
    >
      <div
        className="resize-handle"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize reading pane"
        aria-valuenow={width}
        aria-valuemin={MIN_WIDTH}
        aria-valuemax={MAX_WIDTH}
        tabIndex={0}
        onPointerDown={startResize}
        onKeyDown={resizeByKey}
      />
      <div className="reading-scroll" ref={body}>
        <header>
          <h2>{memory ? memory.title : error ? "Memory unavailable" : "Loading…"}</h2>
          <button type="button" onClick={onClose} aria-label="Close">
            Close
          </button>
        </header>
        {error ? <div className="error-box">This memory could not be loaded: {error}</div> : null}
        {memory ? (
          <div className={stale ? "reading-body stale" : "reading-body"}>
            <div className="chips">
              <span className="chip">{memory.memory_type}</span>
              {memory.team ? <span className="chip">{memory.team}</span> : null}
              {memory.role ? <span className="chip">{memory.role}</span> : null}
              <span className={`chip sensitivity ${memory.sensitivity}`}>{memory.sensitivity}</span>
            </div>
            <dl className="reading-meta">
              {memory.path || memory.source_uri ? (
                <>
                  <dt>Source</dt>
                  <dd className="mono" title={memory.source_uri ?? undefined}>
                    {memory.path ?? memory.source_uri}
                  </dd>
                </>
              ) : null}
              <dt>Stored</dt>
              <dd>{new Date(memory.created_at).toLocaleString()}</dd>
              <dt>Indexed</dt>
              <dd>
                {memory.chunk_count} {memory.chunk_count === 1 ? "chunk" : "chunks"}
                {memory.embedding_model ? "" : ", not embedded yet"}
              </dd>
            </dl>
            <div className="prose" data-testid="reading-content">
              <ReactMarkdown>{memory.content}</ReactMarkdown>
            </div>
            {memory.truncated ? (
              <p className="notice">
                This memory is longer than 50,000 characters; the rest is not shown here.
              </p>
            ) : null}

            <section data-testid="reading-related">
              <h3>Connected memories</h3>
              {groups.length === 0 ? (
                <p className="muted">Nothing is linked to this memory yet.</p>
              ) : (
                groups.map((group) => (
                  <div key={group.kind} className="related-group">
                    <h4>
                      <i className={`link-key ${group.kind}`} aria-hidden="true" />
                      {KIND_LABEL[group.kind]}
                    </h4>
                    <ul>
                      {group.items.map((item) => (
                        <li key={`${item.kind}-${item.id}`}>
                          <button type="button" onClick={() => onNavigate(item.id)}>
                            <span className="ellipsis">{item.title}</span>
                            <span className="mono muted">{strength(item)}</span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))
              )}
            </section>

            <section data-testid="reading-recalls">
              <h3>Recall history</h3>
              {memory.recalls.count === 0 ? (
                <p className="muted">No agent search has returned this memory yet.</p>
              ) : (
                <>
                  <p className="muted">
                    Returned by {memory.recalls.count}{" "}
                    {memory.recalls.count === 1 ? "search" : "searches"}
                    {memory.recalls.last ? `, last ${relativeTime(memory.recalls.last)}` : ""}.
                  </p>
                  <ul className="recalls">
                    {memory.recalls.recent.map((recall) => (
                      <li key={recall.trace_id}>
                        <Link href={`/traces/${recall.trace_id}`} title="Open this search in replay">
                          <span className="ellipsis">“{recall.query || "search"}”</span>
                          <span className="mono muted">
                            #{recall.rank} · {relativeTime(recall.at)}
                          </span>
                        </Link>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </section>
          </div>
        ) : null}
      </div>
    </aside>
  );
}
