"""Native Claude Code lifecycle adapter for Albert automatic memory."""

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
CLAUDE_EVENTS = {
    "session_started",
    "user_goal",
    "assistant_outcome",
    "pre_compaction_checkpoint",
    "session_ended",
    "failure_summary",
}


def _operation_key(kind: str, session: str, identity: str) -> str:
    digest = hashlib.sha256(f"{kind}\0{session}\0{identity}".encode()).hexdigest()
    return f"claude-code:{kind}:{digest}"


class ClaudeState:
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


class ClaudeAdapter:
    """Maps supported Claude Code hooks onto the shared capture SDK contract."""

    def __init__(
        self,
        client: CaptureClient,
        state: ClaudeState,
        *,
        workspace_id: str | None = None,
    ) -> None:
        self.client = client
        self.state = state
        self.workspace_id = workspace_id

    @staticmethod
    def _session(event: dict[str, Any]) -> str:
        value = event.get("session_id")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Claude Code hook requires session_id")
        return value[:300]

    @staticmethod
    def _vcs(event: dict[str, Any]) -> dict[str, Any]:
        cwd = event.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            return {"vcs": "none", "project": "unknown", "include_absolute_path": False}
        return collect_vcs_context(cwd)

    @classmethod
    def _provenance(cls, event: dict[str, Any], hook: str) -> dict[str, Any]:
        vcs = cls._vcs(event)
        return {
            "client": "claude-code",
            "agent": str(event.get("agent_type", "main"))[:200],
            "project": str(vcs.get("project", "unknown"))[:300],
            "branch": str(vcs.get("branch") or vcs.get("ref") or "")[:300] or None,
            "run": str(event.get("prompt_id", ""))[:300] or None,
            "event": hook[:300],
            "source_session": cls._session(event),
            "adapter_version": ADAPTER_VERSION,
            "include_absolute_path": False,
        }

    @classmethod
    def _capture_session(cls, event: dict[str, Any]) -> str:
        session = cls._session(event)
        return _operation_key("start", session, session)

    def _start(self, event: dict[str, Any]) -> DeliveryResult:
        session = self._session(event)
        return self.client.start_session(
            session,
            self._capture_session(event),
            workspace_id=self.workspace_id,
            provenance=self._provenance(event, "SessionStart"),
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
        hook = str(event.get("hook_event_name", ""))
        if hook not in {
            "SessionStart",
            "UserPromptSubmit",
            "PreCompact",
            "Stop",
            "StopFailure",
            "SessionEnd",
        }:
            raise ValueError(f"unsupported Claude Code hook: {hook}")

        self.client.flush()
        results: list[DeliveryResult] = [self._start(event)]
        session = self._session(event)
        capture_session = self._capture_session(event)

        if hook == "SessionStart":
            source = str(event.get("source", "startup"))[:100]
            results.append(
                self._record(
                    event,
                    "session_started",
                    f"Claude Code session started: {source}.",
                    f"{session}:{source}",
                    hook,
                )
            )
            project = self._vcs(event).get("project")
            query = f"Approved project context for {project or 'this Claude Code workspace'}"
            context = self.client.recall(query, provenance=self._provenance(event, hook))
            return {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                },
                "results": [asdict(item) for item in results],
            }

        if hook == "UserPromptSubmit":
            prompt = str(event.get("prompt", ""))[:MAX_SUMMARY_CHARACTERS]
            screened = screen_capture(prompt, {})
            if not prompt.strip() or screened.reasons:
                return {"results": [asdict(item) for item in results]}
            identity = str(event.get("prompt_id") or hashlib.sha256(prompt.encode()).hexdigest())
            results.append(self._record(event, "user_goal", prompt, identity, hook))
            context = self.client.recall(
                screened.content, provenance=self._provenance(event, hook)
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                },
                "results": [asdict(item) for item in results],
            }
        if hook == "PreCompact":
            trigger = str(event.get("trigger", "unknown"))[:100]
            identity = str(event.get("prompt_id") or f"{trigger}:compaction")
            results.append(
                self._record(
                    event,
                    "pre_compaction_checkpoint",
                    f"Claude Code pre-compaction checkpoint: {trigger}.",
                    identity,
                    hook,
                )
            )
            results.append(
                self.client.checkpoint(
                    capture_session,
                    self.state.final_sequence(capture_session),
                    _operation_key("checkpoint", session, identity),
                )
            )
        elif hook == "Stop":
            content = str(event.get("last_assistant_message") or "Claude Code turn completed.")
            identity = str(
                event.get("prompt_id")
                or hashlib.sha256(content[:MAX_SUMMARY_CHARACTERS].encode()).hexdigest()
            )
            results.append(self._record(event, "assistant_outcome", content, identity, hook))
        elif hook == "StopFailure":
            error = str(event.get("error", "unknown"))[:100]
            identity = str(event.get("prompt_id") or error)
            results.append(
                self._record(
                    event,
                    "failure_summary",
                    f"Claude Code turn failed: {error}.",
                    identity,
                    hook,
                )
            )
        elif hook == "SessionEnd":
            reason = str(event.get("reason", "other"))[:100]
            results.append(
                self._record(
                    event,
                    "session_ended",
                    f"Claude Code session ended: {reason}.",
                    session,
                    hook,
                )
            )
            sequence = self.state.final_sequence(capture_session)
            results.append(
                self.client.checkpoint(
                    capture_session,
                    sequence,
                    _operation_key("end-checkpoint", session, session),
                )
            )
            results.append(
                self.client.end_session(
                    capture_session,
                    sequence,
                    _operation_key("end", session, session),
                )
            )

        return {"results": [asdict(item) for item in results]}


def _client_from_environment() -> tuple[CaptureClient, ClaudeState, str | None]:
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
        environment.get("ALBERT_CAPTURE_QUEUE_PATH", "~/.claude/albert/capture.db")
    ).expanduser()
    scope = environment.get("ALBERT_CAPTURE_SCOPE", "claude-code:default")
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
        local_event_classes=CLAUDE_EVENTS,
        client=http,
    )
    return (
        client,
        ClaudeState(queue_path.with_suffix(".claude-state.db")),
        environment.get("ALBERT_WORKSPACE_ID"),
    )


def main() -> None:
    """Process one minimized Claude Code hook envelope from stdin, always fail open."""
    client: CaptureClient | None = None
    try:
        value = json.load(sys.stdin)
        if not isinstance(value, dict):
            raise ValueError("hook envelope must be an object")
        client, state, workspace_id = _client_from_environment()
        adapter = ClaudeAdapter(client, state, workspace_id=workspace_id)
        print(json.dumps(adapter.handle(value), separators=(",", ":")))
    except Exception as exc:
        print(
            json.dumps({"albert_status": "unavailable", "error": type(exc).__name__}),
            file=sys.stderr,
        )
        print("{}")
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
