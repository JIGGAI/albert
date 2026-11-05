"""Native Codex CLI lifecycle adapter for Albert automatic memory."""

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
CODEX_EVENTS = {
    "session_started",
    "user_goal",
    "assistant_outcome",
    "pre_compaction_checkpoint",
}
SUPPORTED_HOOKS = {"SessionStart", "UserPromptSubmit", "PreCompact", "Stop"}


def _operation_key(kind: str, session: str, identity: str) -> str:
    digest = hashlib.sha256(f"{kind}\0{session}\0{identity}".encode()).hexdigest()
    return f"codex:{kind}:{digest}"


class CodexState:
    """Crash-safe sequence allocation for retried Codex hooks."""

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


class CodexAdapter:
    """Maps supported Codex command hooks onto the shared capture SDK contract."""

    def __init__(
        self,
        client: CaptureClient,
        state: CodexState,
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
            raise ValueError("Codex hook requires session_id")
        return value[:300]

    @staticmethod
    def _vcs(event: dict[str, Any]) -> dict[str, Any]:
        cwd = event.get("cwd")
        return collect_vcs_context(cwd) if isinstance(cwd, str) and cwd else collect_vcs_context()

    @classmethod
    def _provenance(cls, event: dict[str, Any]) -> dict[str, Any]:
        vcs = cls._vcs(event)
        return {
            **vcs,
            "client": "codex",
            "agent": str(event.get("agent_id", "main"))[:200],
            "model": str(event.get("model", "unknown"))[:200],
            "event": str(event.get("hook_event_name", "unknown"))[:100],
            "source_session": cls._session(event),
            "adapter_version": ADAPTER_VERSION,
            "include_absolute_path": False,
        }

    def _start(self, event: dict[str, Any]) -> DeliveryResult:
        session = self._capture_session(event)
        provenance = self._provenance(event)
        provenance.pop("event", None)
        return self.client.start_session(
            session,
            session,
            workspace_id=self.workspace_id,
            provenance=provenance,
        )

    @classmethod
    def _capture_session(cls, event: dict[str, Any]) -> str:
        session = cls._session(event)
        return _operation_key("start", session, session)

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

    @staticmethod
    def _context_response(hook: str, context: str) -> dict[str, Any]:
        return {
            "hookSpecificOutput": {
                "hookEventName": hook,
                "additionalContext": context,
            }
        }

    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        hook = str(event.get("hook_event_name", ""))
        if hook not in SUPPORTED_HOOKS:
            raise ValueError(f"unsupported Codex hook: {hook}")

        self.client.flush()
        results: list[DeliveryResult] = [self._start(event)]
        session = self._capture_session(event)

        if hook == "SessionStart":
            results.append(
                self._record(event, "session_started", "Codex session started.", session)
            )
            vcs = self._vcs(event)
            project = str(vcs.get("project", "unknown"))[:300]
            context = self.client.recall(
                f"Codex session context for project {project}",
                provenance=self._provenance(event),
            )
            response = self._context_response(hook, context)
            response["results"] = [asdict(item) for item in results]
            return response

        if hook == "UserPromptSubmit":
            prompt = str(event.get("prompt", ""))[:MAX_SUMMARY_CHARACTERS]
            screened = screen_capture(prompt, {})
            if not prompt.strip() or screened.reasons:
                return {"results": [asdict(item) for item in results]}
            turn_id = str(event.get("turn_id") or hashlib.sha256(prompt.encode()).hexdigest())
            results.append(self._record(event, "user_goal", prompt, turn_id))
            context = self.client.recall(screened.content, provenance=self._provenance(event))
            response = self._context_response(hook, context)
            response["results"] = [asdict(item) for item in results]
            return response

        turn_id = str(event.get("turn_id") or "unknown-turn")
        if hook == "PreCompact":
            trigger = str(event.get("trigger") or "unknown")[:100]
            identity = f"{turn_id}:{trigger}"
            results.append(
                self._record(
                    event,
                    "pre_compaction_checkpoint",
                    f"Codex pre-compaction checkpoint: {trigger} trigger.",
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
        elif hook == "Stop":
            outcome = str(event.get("last_assistant_message") or "").strip()
            if outcome:
                results.append(self._record(event, "assistant_outcome", outcome, turn_id))
            results.append(
                self.client.checkpoint(
                    session,
                    self.state.final_sequence(session),
                    _operation_key("turn-checkpoint", session, turn_id),
                )
            )

        return {"results": [asdict(item) for item in results]}


def _client_from_environment() -> tuple[CaptureClient, CodexState, str | None]:
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
        raise RuntimeError("Albert capture credentials are not configured")
    queue_path = Path(
        environment.get("ALBERT_CAPTURE_QUEUE_PATH", "~/.codex/albert/capture.db")
    ).expanduser()
    scope = environment.get("ALBERT_CAPTURE_SCOPE", "codex:default")
    timeout = max(0.1, min(5.0, float(environment.get("ALBERT_CAPTURE_TIMEOUT", "1.0"))))
    recall_timeout = max(0.1, min(4.5, float(environment.get("ALBERT_RECALL_TIMEOUT", "4.0"))))
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
        local_event_classes=CODEX_EVENTS,
        client=http,
        recall_timeout=recall_timeout,
    )
    return (
        client,
        CodexState(queue_path.with_suffix(".codex-state.db")),
        environment.get("ALBERT_WORKSPACE_ID"),
    )


def main() -> None:
    """Process one Codex command-hook envelope without blocking agent work on failure."""
    client: CaptureClient | None = None
    try:
        value = json.load(sys.stdin)
        if not isinstance(value, dict):
            raise ValueError("hook envelope must be an object")
        client, state, workspace_id = _client_from_environment()
        adapter = CodexAdapter(client, state, workspace_id=workspace_id)
        result = adapter.handle(value)
        result.pop("results", None)
        print(json.dumps(result, separators=(",", ":")))
    except Exception as exc:
        # Codex command hooks treat an empty successful response as a no-op. Keep
        # delivery/configuration failures from interrupting the user's turn, but say
        # what failed (type only, never content) so setup problems can be diagnosed.
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
