"""Native Cursor lifecycle adapter for Albert automatic memory."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from albert.capture_sdk import CaptureClient, DeliveryResult, EncryptedQueue, collect_vcs_context
from albert.capture_security import screen_capture

ADAPTER_VERSION = "0.1.0"
MAX_SUMMARY_CHARACTERS = 4_000
CURSOR_EVENTS = {
    "session_started",
    "user_goal",
    "assistant_outcome",
    "pre_compaction_checkpoint",
    "session_ended",
    "failure_summary",
}
SUPPORTED_HOOKS = {
    "sessionStart",
    "beforeSubmitPrompt",
    "preCompact",
    "afterAgentResponse",
    "stop",
    "sessionEnd",
}


def _operation_key(kind: str, session: str, identity: str) -> str:
    digest = hashlib.sha256(f"{kind}\0{session}\0{identity}".encode()).hexdigest()
    return f"cursor:{kind}:{digest}"


class CursorState:
    """Crash-safe sequence allocation for retried Cursor hooks."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS operations (
              operation_key TEXT PRIMARY KEY, session_key TEXT NOT NULL,
              sequence INTEGER NOT NULL, event_class TEXT NOT NULL,
              UNIQUE(session_key, sequence));
            """)
        os.chmod(self.path, 0o600)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def sequence(self, session: str, operation_key: str, event_class: str) -> int:
        with self._lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT sequence FROM operations WHERE operation_key=?", (operation_key,)
            ).fetchone()
            if existing:
                return int(existing[0])
            row = db.execute(
                "SELECT coalesce(max(sequence),0) FROM operations WHERE session_key=?", (session,)
            ).fetchone()
            sequence = int(row[0]) + 1
            db.execute(
                "INSERT INTO operations VALUES(?,?,?,?)",
                (operation_key, session, sequence, event_class),
            )
            return sequence

    def final_sequence(self, session: str) -> int:
        with self._lock, self._db() as db:
            row = db.execute(
                "SELECT coalesce(max(sequence),0) FROM operations WHERE session_key=?", (session,)
            ).fetchone()
        return int(row[0])


class CursorAdapter:
    """Map supported Cursor command hooks onto the shared capture SDK."""

    def __init__(
        self,
        client: CaptureClient,
        state: CursorState,
        *,
        workspace_id: str | None = None,
    ) -> None:
        self.client = client
        self.state = state
        self.workspace_id = workspace_id

    @staticmethod
    def _session(event: dict[str, Any]) -> str:
        value = event.get("conversation_id") or event.get("session_id")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Cursor hook requires conversation_id or session_id")
        return value[:300]

    @staticmethod
    def _vcs(event: dict[str, Any]) -> dict[str, Any]:
        roots = event.get("workspace_roots")
        root = roots[0] if isinstance(roots, list) and roots and isinstance(roots[0], str) else None
        return collect_vcs_context(root) if root else collect_vcs_context()

    @classmethod
    def _provenance(cls, event: dict[str, Any]) -> dict[str, Any]:
        return {
            **cls._vcs(event),
            "client": "cursor",
            "agent": "background" if event.get("is_background_agent") else "interactive",
            "model": str(event.get("model", "unknown"))[:200],
            "event": str(event.get("hook_event_name", "unknown"))[:100],
            "source_session": cls._session(event),
            "adapter_version": ADAPTER_VERSION,
            "client_version": str(event.get("cursor_version", "unknown"))[:100],
            "include_absolute_path": False,
        }

    @classmethod
    def _capture_session(cls, event: dict[str, Any]) -> str:
        session = cls._session(event)
        return _operation_key("start", session, session)

    def _start(self, event: dict[str, Any]) -> DeliveryResult:
        session = self._capture_session(event)
        return self.client.start_session(
            session,
            session,
            workspace_id=self.workspace_id,
            provenance=self._provenance(event),
        )

    def _record(
        self,
        event: dict[str, Any],
        event_class: str,
        content: str,
        identity: str,
    ) -> DeliveryResult:
        screened = screen_capture(content[:MAX_SUMMARY_CHARACTERS], {})
        if screened.reasons:
            return DeliveryResult("dropped", "secret_detected")
        session = self._capture_session(event)
        key = _operation_key(event_class, session, identity)
        sequence = self.state.sequence(session, key, event_class)
        return self.client.record_event(
            session,
            sequence,
            event_class,
            screened.content,
            key,
            provenance=self._provenance(event),
        )

    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        hook = str(event.get("hook_event_name", ""))
        if hook not in SUPPORTED_HOOKS:
            raise ValueError(f"unsupported Cursor hook: {hook}")

        self.client.flush()
        results: list[DeliveryResult] = [self._start(event)]
        session = self._capture_session(event)
        generation = str(event.get("generation_id") or "unknown-generation")

        if hook == "sessionStart":
            results.append(
                self._record(event, "session_started", "Cursor session started.", session)
            )
            vcs = self._vcs(event)
            project = str(vcs.get("project", "unknown"))[:300]
            context = self.client.recall(
                f"Cursor session context for project {project}",
                project_ref=project,
                branch_ref=str(vcs.get("branch")) if vcs.get("branch") else None,
            )
            return {"additional_context": context, "results": [asdict(item) for item in results]}

        if hook == "beforeSubmitPrompt":
            prompt = str(event.get("prompt", ""))[:MAX_SUMMARY_CHARACTERS]
            screened = screen_capture(prompt, {})
            if prompt.strip() and not screened.reasons:
                results.append(self._record(event, "user_goal", prompt, generation))
            return {"continue": True, "results": [asdict(item) for item in results]}

        if hook == "preCompact":
            trigger = str(event.get("trigger") or "unknown")[:100]
            messages = max(0, int(event.get("message_count", 0)))
            tokens = max(0, int(event.get("context_tokens", 0)))
            identity = (
                f"{generation}:{trigger}:{messages}:{int(bool(event.get('is_first_compaction')))}"
            )
            results.append(
                self._record(
                    event,
                    "pre_compaction_checkpoint",
                    f"Cursor pre-compaction checkpoint: {trigger} trigger, "
                    f"{messages} messages, {tokens} context tokens.",
                    identity,
                )
            )
            results.append(
                self.client.checkpoint(
                    session,
                    self.state.final_sequence(session),
                    _operation_key("pre-compact-checkpoint", session, identity),
                )
            )
        elif hook == "afterAgentResponse":
            text = str(event.get("text", "")).strip()
            if text:
                results.append(self._record(event, "assistant_outcome", text, generation))
        elif hook == "stop":
            status = str(event.get("status") or "unknown")[:100]
            if status == "error":
                results.append(
                    self._record(
                        event,
                        "failure_summary",
                        "Cursor reported that the agent loop stopped with error status.",
                        generation,
                    )
                )
            results.append(
                self.client.checkpoint(
                    session,
                    self.state.final_sequence(session),
                    _operation_key("turn-checkpoint", session, generation),
                )
            )
        elif hook == "sessionEnd":
            reason = str(event.get("reason") or "unknown")[:100]
            results.append(
                self._record(
                    event,
                    "session_ended",
                    f"Cursor session ended: {reason}.",
                    self._session(event),
                )
            )
            sequence = self.state.final_sequence(session)
            results.append(
                self.client.checkpoint(
                    session,
                    sequence,
                    _operation_key("end-checkpoint", session, self._session(event)),
                )
            )
            results.append(
                self.client.end_session(
                    session,
                    sequence,
                    _operation_key("end", session, self._session(event)),
                )
            )

        return {"results": [asdict(item) for item in results]}


def _client_from_environment() -> tuple[CaptureClient, CursorState]:
    base_url = os.environ.get("ALBERT_API_URL", "http://127.0.0.1:8080")
    token = os.environ.get("ALBERT_CAPTURE_TOKEN")
    queue_key = os.environ.get("ALBERT_CAPTURE_QUEUE_KEY")
    if not token or not queue_key:
        raise RuntimeError("Albert capture credentials are not configured")
    queue_path = Path(
        os.environ.get("ALBERT_CAPTURE_QUEUE_PATH", "~/.cursor/albert/capture.db")
    ).expanduser()
    scope = os.environ.get("ALBERT_CAPTURE_SCOPE", "cursor:default")
    timeout = max(0.1, min(5.0, float(os.environ.get("ALBERT_CAPTURE_TIMEOUT", "1.0"))))
    queue = EncryptedQueue(queue_path, queue_key, scope)
    http = httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    client = CaptureClient(
        base_url,
        token,
        queue,
        local_event_classes=CURSOR_EVENTS,
        client=http,
    )
    return client, CursorState(queue_path.with_suffix(".cursor-state.db"))


def main() -> None:
    """Process one Cursor command-hook envelope without blocking agent work."""
    client: CaptureClient | None = None
    try:
        value = json.load(sys.stdin)
        if not isinstance(value, dict):
            raise ValueError("hook envelope must be an object")
        client, state = _client_from_environment()
        adapter = CursorAdapter(client, state, workspace_id=os.environ.get("ALBERT_WORKSPACE_ID"))
        result = adapter.handle(value)
        result.pop("results", None)
        print(json.dumps(result, separators=(",", ":")))
    except Exception:
        print("{}")
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
