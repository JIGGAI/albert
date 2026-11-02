"""Safe installer, doctor, and uninstaller for Albert capture adapters."""

from __future__ import annotations

import getpass
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import typer

from albert.capture_sdk import CaptureClient, EncryptedQueue, generate_queue_key
from albert.openclaw_adapter import OPENCLAW_EVENTS

app = typer.Typer(help="Connect supported agent hosts directly to an Albert Brain.")
PLUGIN_ID = "albert-memory"
MIN_OPENCLAW = (2026, 9, 6)
MAX_OPENCLAW = (2027, 0, 0)
VERSION_PATTERN = re.compile(r"OpenClaw\s+(\d+)\.(\d+)\.(\d+)")


class InstallError(RuntimeError):
    """An expected, safely reportable installer failure."""


@dataclass(frozen=True)
class InstallPaths:
    root: Path

    @property
    def credentials(self) -> Path:
        return self.root / "credentials.env"

    @property
    def wrapper(self) -> Path:
        return self.root / "bin" / "albert-openclaw-adapter"

    @property
    def marker(self) -> Path:
        return self.root / "install.json"

    @property
    def queue(self) -> Path:
        return self.root / "capture.db"


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _checked(runner: Runner, command: list[str], label: str) -> subprocess.CompletedProcess[str]:
    result = runner(command)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise InstallError(f"{label} failed{suffix}")
    return result


def _detect_openclaw(runner: Runner) -> tuple[int, int, int]:
    result = _checked(runner, ["openclaw", "--version"], "OpenClaw detection")
    match = VERSION_PATTERN.search(result.stdout)
    if not match:
        raise InstallError("unsupported OpenClaw version output")
    version = tuple(int(value) for value in match.groups())
    if not MIN_OPENCLAW <= version < MAX_OPENCLAW:
        rendered = ".".join(str(value) for value in version)
        raise InstallError(f"OpenClaw {rendered} is unsupported; require >=2026.9.6 and <2027")
    return version


def _validate_secret(value: str) -> str:
    if not value or any(character in value for character in "\r\n\0"):
        raise InstallError("credential must be non-empty and contain no control characters")
    return value


def _write_private(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, mode)


def _credentials_text(
    api_url: str, token: str, queue_key: str, queue_path: Path, workspace_id: str | None
) -> str:
    values = {
        "ALBERT_API_URL": api_url.rstrip("/"),
        "ALBERT_CAPTURE_TOKEN": token,
        "ALBERT_CAPTURE_QUEUE_KEY": queue_key,
        "ALBERT_CAPTURE_QUEUE_PATH": str(queue_path),
        "ALBERT_CAPTURE_SCOPE": "openclaw:default",
    }
    if workspace_id:
        values["ALBERT_WORKSPACE_ID"] = workspace_id
    return json.dumps(values, sort_keys=True) + "\n"


def _wrapper_text(credentials: Path) -> str:
    python = shlex.quote(sys.executable)
    secret_file = shlex.quote(str(credentials))
    return (
        "#!/bin/sh\n"
        f"export ALBERT_CAPTURE_CREDENTIAL_FILE={secret_file}\n"
        f"exec {python} -m albert.openclaw_adapter\n"
    )


def _brain_check(
    api_url: str,
    token: str,
    queue_key: str,
    workspace_id: str | None,
    *,
    smoke: bool,
) -> None:
    base_url = api_url.rstrip("/")
    try:
        with httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        ) as http:
            ready = http.get("/v1/health/ready")
            if not ready.is_success:
                raise InstallError(f"Brain readiness check failed ({ready.status_code})")
            with tempfile.TemporaryDirectory(prefix="albert-install-check-") as temporary:
                queue = EncryptedQueue(Path(temporary) / "capture.db", queue_key, "installer:smoke")
                client = CaptureClient(
                    base_url,
                    token,
                    queue,
                    local_event_classes=OPENCLAW_EVENTS,
                    client=http,
                )
                if not client.negotiate():
                    raise InstallError("Brain lacks the required capture/recall capabilities")
                if not smoke:
                    return
                nonce = uuid.uuid4().hex
                session_key = f"installer:start:{nonce}"
                start = client.start_session(
                    session_key,
                    session_key,
                    workspace_id=workspace_id,
                    provenance={"client": "installer", "event": "live_smoke"},
                )
                event = client.record_event(
                    session_key,
                    1,
                    "user_goal",
                    f"Albert installer smoke {nonce}",
                    f"installer:event:{nonce}",
                    provenance={"client": "installer", "event": "live_smoke"},
                )
                recalled = client.recall(f"Albert installer smoke {nonce}", max_items=1)
                end = client.end_session(session_key, 1, f"installer:end:{nonce}")
                if any(item.status != "delivered" for item in (start, event, end)):
                    raise InstallError("live capture smoke test was rejected")
                if "ALBERT REFERENCE CONTEXT" not in recalled:
                    raise InstallError("live recall smoke test returned an invalid response")
    except httpx.HTTPError as exc:
        raise InstallError(f"direct Brain connection failed ({type(exc).__name__})") from None


def install_openclaw(
    *,
    api_url: str,
    token: str,
    workspace_id: str | None,
    paths: InstallPaths,
    runner: Runner = _run,
    brain_check: Callable[[str, str, str, str | None], None] | None = None,
) -> None:
    version = _detect_openclaw(runner)
    token = _validate_secret(token)
    api_url = api_url.rstrip("/")
    if not api_url.startswith(("http://", "https://")):
        raise InstallError("Brain URL must use http:// or https://")

    queue_key = generate_queue_key()
    check = brain_check or (
        lambda url, secret, key, workspace: _brain_check(url, secret, key, workspace, smoke=True)
    )
    check(api_url, token, queue_key, workspace_id)

    config_path = Path(
        _checked(runner, ["openclaw", "config", "file"], "config discovery").stdout.strip()
    ).expanduser()
    config_existed = config_path.exists()
    config_before = config_path.read_bytes() if config_existed else None
    config_mode = stat.S_IMODE(config_path.stat().st_mode) if config_existed else 0o600
    backup = paths.root.with_name(f".{paths.root.name}.rollback-{uuid.uuid4().hex}")
    staging = paths.root.with_name(f".{paths.root.name}.staging-{uuid.uuid4().hex}")
    plugin_path = Path(__file__).with_name("openclaw_plugin")
    try:
        staged = InstallPaths(staging)
        _write_private(
            staged.credentials,
            _credentials_text(api_url, token, queue_key, paths.queue, workspace_id),
            0o600,
        )
        _write_private(staged.wrapper, _wrapper_text(paths.credentials), 0o700)
        marker = {
            "owner": "albert-memory",
            "host": "openclaw",
            "api_url": api_url,
            "openclaw_version": ".".join(str(value) for value in version),
        }
        _write_private(staged.marker, json.dumps(marker, sort_keys=True) + "\n", 0o600)

        if paths.root.exists():
            paths.root.rename(backup)
        staging.rename(paths.root)
        _checked(
            runner,
            [
                "openclaw",
                "plugins",
                "install",
                "--link",
                "--force",
                "--accept-capabilities",
                str(plugin_path),
            ],
            "OpenClaw plugin install",
        )
        entry = {
            "enabled": True,
            "hooks": {"allowPromptInjection": True, "allowConversationAccess": True},
            "config": {"command": str(paths.wrapper), "timeoutMs": 1800},
        }
        _checked(
            runner,
            [
                "openclaw",
                "config",
                "set",
                f"plugins.entries.{PLUGIN_ID}",
                json.dumps(entry, separators=(",", ":")),
                "--strict-json",
            ],
            "OpenClaw plugin configuration",
        )
        _checked(runner, ["openclaw", "config", "validate"], "OpenClaw config validation")
        _checked(
            runner,
            ["openclaw", "plugins", "inspect", PLUGIN_ID, "--runtime", "--json"],
            "OpenClaw runtime inspection",
        )
    except Exception:
        if paths.root.exists():
            shutil.rmtree(paths.root)
        if backup.exists():
            backup.rename(paths.root)
        if config_before is not None:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = config_path.with_name(f".{config_path.name}.albert-rollback")
            temporary.write_bytes(config_before)
            os.chmod(temporary, config_mode)
            os.replace(temporary, config_path)
        elif not config_existed and config_path.exists():
            config_path.unlink()
        if staging.exists():
            shutil.rmtree(staging)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)


def _load_credentials(paths: InstallPaths) -> dict[str, str]:
    if not paths.marker.exists() or not paths.credentials.exists():
        raise InstallError("no Albert-owned OpenClaw installation found")
    marker = json.loads(paths.marker.read_text(encoding="utf-8"))
    if marker.get("owner") != "albert-memory":
        raise InstallError("installation ownership marker is invalid")
    values = json.loads(paths.credentials.read_text(encoding="utf-8"))
    if not isinstance(values, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in values.items()
    ):
        raise InstallError("credential file is invalid")
    return values


def doctor_openclaw(paths: InstallPaths, *, smoke: bool = False, runner: Runner = _run) -> None:
    _detect_openclaw(runner)
    values = _load_credentials(paths)
    _checked(runner, ["openclaw", "config", "validate"], "OpenClaw config validation")
    _checked(
        runner,
        ["openclaw", "plugins", "inspect", PLUGIN_ID, "--runtime", "--json"],
        "OpenClaw runtime inspection",
    )
    _brain_check(
        values["ALBERT_API_URL"],
        values["ALBERT_CAPTURE_TOKEN"],
        values["ALBERT_CAPTURE_QUEUE_KEY"],
        values.get("ALBERT_WORKSPACE_ID"),
        smoke=smoke,
    )


def uninstall_openclaw(paths: InstallPaths, *, runner: Runner = _run) -> None:
    _load_credentials(paths)
    result = runner(["openclaw", "plugins", "uninstall", PLUGIN_ID, "--force"])
    if result.returncode:
        _checked(
            runner,
            ["openclaw", "config", "unset", f"plugins.entries.{PLUGIN_ID}"],
            "OpenClaw config cleanup",
        )
    if paths.root.exists():
        shutil.rmtree(paths.root)


def _default_paths() -> InstallPaths:
    return InstallPaths(Path("~/.openclaw/albert").expanduser())


@app.command("install")
def install_command(
    api_url: str = typer.Option(..., "--api-url", help="Direct self-hosted or managed Brain URL."),
    workspace_id: str | None = typer.Option(None, "--workspace-id"),
) -> None:
    """Install the first ready adapter target (OpenClaw)."""
    try:
        token = getpass.getpass("Albert capture token: ")
        install_openclaw(
            api_url=api_url,
            token=token,
            workspace_id=workspace_id,
            paths=_default_paths(),
        )
        typer.echo("Albert Memory installed for OpenClaw; live capture and recall passed.")
    except InstallError as exc:
        typer.echo(f"Install failed safely: {exc}", err=True)
        raise typer.Exit(1) from None


@app.command("doctor")
def doctor_command(smoke: bool = typer.Option(False, "--smoke")) -> None:
    """Verify the installed host, configuration, Brain, and optional live smoke flow."""
    try:
        doctor_openclaw(_default_paths(), smoke=smoke)
        typer.echo("Albert Memory doctor passed.")
    except InstallError as exc:
        typer.echo(f"Doctor failed: {exc}", err=True)
        raise typer.Exit(1) from None


@app.command("uninstall")
def uninstall_command() -> None:
    """Remove only Albert-owned OpenClaw configuration and files."""
    try:
        uninstall_openclaw(_default_paths())
        typer.echo("Albert Memory removed; non-Albert OpenClaw settings were preserved.")
    except InstallError as exc:
        typer.echo(f"Uninstall failed safely: {exc}", err=True)
        raise typer.Exit(1) from None


if __name__ == "__main__":
    app()
