"""Native OpenClaw lifecycle adapter for Albert automatic memory."""

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

from albert.capture_sdk import CaptureClient, DeliveryResult, EncryptedQueue
from albert.capture_security import screen_capture

ADAPTER_VERSION = "0.1.0"
MAX_SUMMARY_CHARACTERS = 4_000
OPENCLAW_EVENTS = {
    "session_started",
    "user_goal",
    "assistant_outcome",
    "pre_compaction_checkpoint",
    "session_ended",
    "failure_summary",
}


def _operation_key(kind: str, session: str, identity: str) -> str:
    value = hashlib.sha256(f"{kind}\0{session}\0{identity}".encode()).hexdigest()
    return f"openclaw:{kind}:{value}"


class OpenClawState:
    """Crash-safe sequence allocation and hook retry deduplication."""

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


class OpenClawAdapter:
    """Maps supported OpenClaw hooks onto the shared capture SDK contract."""

    def __init__(
        self,
        client: CaptureClient,
        state: OpenClawState,
        *,
        workspace_id: str | None = None,
    ) -> None:
        self.client = client
        self.state = state
        self.workspace_id = workspace_id

    @staticmethod
    def _session(event: dict[str, Any]) -> str:
        value = event.get("session_key") or event.get("session_id")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("OpenClaw hook requires session_key or session_id")
        return value[:300]

    @staticmethod
    def _provenance(event: dict[str, Any], hook: str) -> dict[str, Any]:
        return {
            "client": "openclaw",
            "agent": str(event.get("agent_id", "unknown"))[:200],
            "project": str(event.get("project_ref", ""))[:300] or None,
            "branch": str(event.get("branch_ref", ""))[:300] or None,
            "run": str(event.get("run_id", ""))[:300] or None,
            "event": hook[:300],
            "source_session": OpenClawAdapter._session(event),
            "adapter_version": ADAPTER_VERSION,
            "include_absolute_path": False,
        }

    @staticmethod
    def _capture_session(event: dict[str, Any]) -> str:
        session = OpenClawAdapter._session(event)
        identity = str(event.get("session_id") or session)
        return _operation_key("start", session, identity)

    def _start(self, event: dict[str, Any]) -> DeliveryResult:
        session = self._session(event)
        key = self._capture_session(event)
        return self.client.start_session(
            session,
            key,
            workspace_id=self.workspace_id,
            provenance=self._provenance(event, "session_start"),
        )

    def _record(
        self,
        event: dict[str, Any],
        event_class: str,
        content: str,
        identity: str,
        hook: str,
    ) -> DeliveryResult:
        screened = screen_capture(content[:MAX_SUMMARY_CHARACTERS], {})
        if screened.reasons:
            return DeliveryResult("dropped", "secret_detected")
        capture_session = self._capture_session(event)
        key = _operation_key(event_class, capture_session, identity)
        sequence = self.state.sequence(capture_session, key, event_class)
        return self.client.record_event(
            capture_session,
            sequence,
            event_class,
            screened.content,
            key,
            provenance=self._provenance(event, hook),
        )

    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        hook = str(event.get("hook", ""))
        if hook not in {
            "session_start",
            "before_prompt_build",
            "before_compaction",
            "agent_end",
            "session_end",
        }:
            raise ValueError(f"unsupported OpenClaw hook: {hook}")

        self.client.flush()
        started = self._start(event)
        results: list[DeliveryResult] = [started]
        session = self._session(event)
        capture_session = self._capture_session(event)

        if hook == "session_start":
            results.append(
                self._record(
                    event,
                    "session_started",
                    "OpenClaw session started.",
                    str(event.get("session_id") or session),
                    hook,
                )
            )
        elif hook == "before_prompt_build":
            query = str(event.get("current_user_message", ""))[:MAX_SUMMARY_CHARACTERS]
            screened = screen_capture(query, {})
            if not query.strip() or screened.reasons:
                return {"results": [asdict(item) for item in results]}
            identity = str(event.get("message_id") or event.get("run_id") or query)
            results.append(self._record(event, "user_goal", query, identity, hook))
            context = self.client.recall(
                screened.content, provenance=self._provenance(event, hook)
            )
            return {"context": context, "results": [asdict(item) for item in results]}
        elif hook == "before_compaction":
            identity = str(event.get("compaction_id") or event.get("run_id") or "compaction")
            message_count = max(0, int(event.get("message_count", 0)))
            token_count = max(0, int(event.get("token_count", 0)))
            results.append(
                self._record(
                    event,
                    "pre_compaction_checkpoint",
                    f"OpenClaw pre-compaction checkpoint: {message_count} messages, "
                    f"{token_count} estimated tokens.",
                    identity,
                    hook,
                )
            )
            sequence = self.state.final_sequence(capture_session)
            results.append(
                self.client.checkpoint(
                    capture_session,
                    sequence,
                    _operation_key("checkpoint", session, identity),
                )
            )
        elif hook == "agent_end":
            run_id = str(event.get("run_id") or "run")
            success = bool(event.get("success"))
            event_class = "assistant_outcome" if success else "failure_summary"
            fallback = (
                "OpenClaw run completed successfully."
                if success
                else "OpenClaw run failed without an approved failure summary."
            )
            content = str(event.get("outcome") or fallback)
            results.append(self._record(event, event_class, content, run_id, hook))
        elif hook == "session_end":
            identity = str(event.get("session_id") or session)
            reason = str(event.get("reason") or "unknown")[:100]
            results.append(
                self._record(
                    event,
                    "session_ended",
                    f"OpenClaw session ended: {reason}.",
                    identity,
                    hook,
                )
            )
            sequence = self.state.final_sequence(capture_session)
            results.append(
                self.client.checkpoint(
                    capture_session,
                    sequence,
                    _operation_key("end-checkpoint", session, identity),
                )
            )
            results.append(
                self.client.end_session(
                    capture_session,
                    sequence,
                    _operation_key("end", session, identity),
                )
            )

        return {"results": [asdict(item) for item in results]}


def _client_from_environment() -> tuple[CaptureClient, OpenClawState, str | None]:
    environment = dict(os.environ)
    credential_file = environment.get("ALBERT_CAPTURE_CREDENTIAL_FILE")
    if credential_file:
        path = Path(credential_file)
        metadata = path.lstat()
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Albert credential file must be a regular file")
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise RuntimeError("Albert credential file ownership or permissions are unsafe")
        values = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in values.items()
        ):
            raise RuntimeError("Albert credential file is invalid")
        environment.update(values)
    base_url = environment.get("ALBERT_API_URL", "http://127.0.0.1:8080")
    token = environment.get("ALBERT_CAPTURE_TOKEN")
    queue_key = environment.get("ALBERT_CAPTURE_QUEUE_KEY")
    if not token or not queue_key:
        raise RuntimeError("ALBERT_CAPTURE_TOKEN and ALBERT_CAPTURE_QUEUE_KEY are required")
    queue_path = Path(
        environment.get("ALBERT_CAPTURE_QUEUE_PATH", "~/.openclaw/albert/capture.db")
    ).expanduser()
    scope = environment.get("ALBERT_CAPTURE_SCOPE", "openclaw:default")
    timeout = max(0.1, min(5.0, float(environment.get("ALBERT_CAPTURE_TIMEOUT", "1.0"))))
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
        local_event_classes=OPENCLAW_EVENTS,
        client=http,
    )
    return (
        client,
        OpenClawState(queue_path.with_suffix(".openclaw-state.db")),
        environment.get("ALBERT_WORKSPACE_ID"),
    )


def main() -> None:
    """Process one minimized JSON hook envelope from stdin."""
    client: CaptureClient | None = None
    try:
        value = json.load(sys.stdin)
        if not isinstance(value, dict):
            raise ValueError("hook envelope must be an object")
        client, state, workspace_id = _client_from_environment()
        adapter = OpenClawAdapter(client, state, workspace_id=workspace_id)
        print(json.dumps(adapter.handle(value), separators=(",", ":")))
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
