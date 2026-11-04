"""Transactional lifecycle management for the supported Cursor hook target."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import typer

from albert.capture_installer import InstallError, _brain_check, _validate_secret
from albert.capture_sdk import generate_queue_key
from albert.cursor_adapter import CURSOR_EVENTS
from albert.host_targets import HostTargetError, assert_owned_install_root, negotiate_host_target

app = typer.Typer(help="Securely install and operate Albert for Cursor.")
PLUGIN_ID = "albert-memory"
VERSION_PATTERN = re.compile(r"(?:cursor-agent\s+|cursor\s+)?v?(\d+)\.(\d+)\.(\d+)", re.I)
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class CursorInstallPaths:
    root: Path
    hooks_config: Path
    rule_config: Path

    @property
    def credentials(self) -> Path:
        return self.root / "credentials.json"

    @property
    def marker(self) -> Path:
        return self.root / "install.json"

    @property
    def adapter(self) -> Path:
        return self.root / "bin" / "albert-cursor-adapter"

    @property
    def queue(self) -> Path:
        return self.root / "capture.db"


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _detect_cursor(runner: Runner) -> tuple[int, int, int]:
    result = runner(["cursor-agent", "--version"])
    if result.returncode:
        raise InstallError("Cursor detection failed")
    match = VERSION_PATTERN.search(result.stdout)
    if not match:
        raise InstallError("unsupported Cursor version output")
    version = tuple(int(value) for value in match.groups())
    try:
        negotiate_host_target("cursor", version, {"capture", "recall"})
    except HostTargetError as exc:
        raise InstallError(str(exc)) from None
    return version


def _write_private(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, mode)


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    if path.is_symlink():
        raise InstallError("Cursor hooks configuration must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.albert-{uuid.uuid4().hex}")
    try:
        _write_private(temporary, content, mode)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _credential_text(
    api_url: str, token: str, queue_key: str, queue_path: Path, workspace_id: str | None
) -> str:
    values = {
        "ALBERT_API_URL": api_url.rstrip("/"),
        "ALBERT_CAPTURE_QUEUE_KEY": queue_key,
        "ALBERT_CAPTURE_QUEUE_PATH": str(queue_path),
        "ALBERT_CAPTURE_SCOPE": "cursor:default",
        "ALBERT_CAPTURE_TOKEN": token,
    }
    if workspace_id:
        values["ALBERT_WORKSPACE_ID"] = workspace_id
    return json.dumps(values, sort_keys=True) + "\n"


def _adapter_text(credentials: Path) -> str:
    return (
        "#!/bin/sh\n"
        f"export ALBERT_CAPTURE_CREDENTIAL_FILE={shlex.quote(str(credentials))}\n"
        f"exec {shlex.quote(sys.executable)} -m albert.cursor_adapter\n"
    )


def _load_install(
    paths: CursorInstallPaths, *, require_credentials: bool = True
) -> tuple[dict, dict]:
    if not paths.marker.exists() or (require_credentials and not paths.credentials.exists()):
        raise InstallError("no Albert-owned Cursor installation found")
    try:
        marker = json.loads(paths.marker.read_text(encoding="utf-8"))
        values = (
            json.loads(paths.credentials.read_text(encoding="utf-8"))
            if paths.credentials.exists()
            else {}
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError("Cursor installation metadata is invalid") from exc
    if marker.get("owner") != PLUGIN_ID or marker.get("host") != "cursor":
        raise InstallError("installation ownership marker is invalid")
    if not isinstance(values, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in values.items()
    ):
        raise InstallError("credential file is invalid")
    return marker, values


def _load_hooks(path: Path) -> dict:
    if path.is_symlink():
        raise InstallError("Cursor hooks configuration must not be a symlink")
    if not path.exists():
        return {"version": 1, "hooks": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError("Cursor hooks configuration is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or not isinstance(payload.get("hooks"), dict)
    ):
        raise InstallError("Cursor hooks configuration is malformed")
    return payload


def _owned_commands(payload: dict) -> list[str]:
    commands: list[str] = []
    try:
        for hooks in payload["hooks"].values():
            for hook in hooks:
                command = hook.get("command")
                if isinstance(command, str) and "albert-cursor-adapter" in command:
                    commands.append(command)
    except (AttributeError, KeyError, TypeError) as exc:
        raise InstallError("Cursor hooks configuration is malformed") from exc
    return commands


def _configured_hooks(source: Path, adapter: Path) -> dict[str, list[dict]]:
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
        hooks = payload["hooks"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise InstallError("Cursor hook fixture is malformed") from exc
    if set(payload) != {"version", "hooks"} or payload["version"] != 1:
        raise InstallError("Cursor hook fixture schema is unsupported")
    if set(hooks) != {
        "sessionStart",
        "beforeSubmitPrompt",
        "preCompact",
        "afterAgentResponse",
        "stop",
        "sessionEnd",
    }:
        raise InstallError("Cursor hook fixture contains unsupported events")
    for entries in hooks.values():
        if not isinstance(entries, list) or len(entries) != 1:
            raise InstallError("Cursor hook fixture is untrusted")
        for hook in entries:
            if set(hook) != {"command"} or hook.get("command") != "albert-cursor-adapter":
                raise InstallError("Cursor hook fixture is untrusted")
            hook["command"] = str(adapter)
    return hooks


def _merge_hooks(current: dict, owned: dict[str, list[dict]], adapter: Path) -> dict:
    expected = str(adapter)
    existing = _owned_commands(current)
    if existing and any(command != expected for command in existing):
        raise InstallError("Cursor hooks contain a foreign or stale Albert entry")
    merged = json.loads(json.dumps(current))
    for event, hooks in owned.items():
        entries = merged["hooks"].setdefault(event, [])
        entries[:] = [
            hook
            for hook in entries
            if not (isinstance(hook, dict) and hook.get("command") == expected)
        ]
        entries.extend(hooks)
    return merged


def _remove_owned_hooks(current: dict, adapter: Path) -> dict:
    expected = str(adapter)
    existing = _owned_commands(current)
    if any(command != expected for command in existing):
        raise InstallError("Cursor hooks contain a foreign or modified Albert entry")
    updated = json.loads(json.dumps(current))
    for event, entries in list(updated["hooks"].items()):
        kept = [
            hook
            for hook in entries
            if not (isinstance(hook, dict) and hook.get("command") == expected)
        ]
        if kept:
            updated["hooks"][event] = kept
        else:
            del updated["hooks"][event]
    return updated


def install_cursor(
    *,
    api_url: str,
    token: str,
    workspace_id: str | None,
    paths: CursorInstallPaths,
    runner: Runner = _run,
    brain_check: Callable[[str, str, str, str | None], None] | None = None,
    hook_source: Path | None = None,
    rule_source: Path | None = None,
) -> None:
    """Validate, stage, atomically replace, and prove a Cursor installation."""
    version = _detect_cursor(runner)
    try:
        target = negotiate_host_target(
            "cursor", version, {"capture", "recall", "rollback", "uninstall", "upgrade"}
        )
        assert_owned_install_root(paths.root, expected_marker=target.ownership["marker"])
    except HostTargetError as exc:
        raise InstallError(str(exc)) from None
    token = _validate_secret(token)
    api_url = api_url.rstrip("/")
    if not api_url.startswith(("http://", "https://")):
        raise InstallError("Brain URL must use http:// or https://")
    current_hooks = _load_hooks(paths.hooks_config)
    previous = _load_install(paths)[1] if paths.root.exists() else {}
    queue_key = previous.get("ALBERT_CAPTURE_QUEUE_KEY") or generate_queue_key()
    check = brain_check or (
        lambda url, secret, key, workspace: _brain_check(
            url, secret, key, workspace, smoke=True, event_classes=CURSOR_EVENTS
        )
    )
    check(api_url, token, queue_key, workspace_id)

    source = hook_source or Path(__file__).with_name("cursor_hooks") / "hooks.json"
    owned_hooks = _configured_hooks(source, paths.adapter)
    merged_hooks = _merge_hooks(current_hooks, owned_hooks, paths.adapter)
    canonical_rule = Path(__file__).with_name("cursor_rules") / "albert-memory.mdc"
    packaged_rule = rule_source or canonical_rule
    try:
        rule_text = packaged_rule.read_text(encoding="utf-8")
        canonical_rule_text = canonical_rule.read_text(encoding="utf-8")
    except OSError as exc:
        raise InstallError("Cursor rule fixture is unavailable") from exc
    if rule_text != canonical_rule_text:
        raise InstallError("Cursor rule fixture is untrusted")
    if paths.rule_config.is_symlink():
        raise InstallError("Cursor rule configuration must not be a symlink")
    original_config = paths.hooks_config.read_bytes() if paths.hooks_config.exists() else None
    original_rule = paths.rule_config.read_bytes() if paths.rule_config.exists() else None
    if original_rule is not None and original_rule.decode("utf-8") != rule_text:
        raise InstallError("Cursor rule path contains foreign or modified content")
    backup = paths.root.with_name(f".{paths.root.name}.rollback-{uuid.uuid4().hex}")
    staging = paths.root.with_name(f".{paths.root.name}.staging-{uuid.uuid4().hex}")
    staged = CursorInstallPaths(staging, paths.hooks_config, paths.rule_config)
    swapped = False
    hooks_changed = False
    rule_changed = False
    try:
        _write_private(
            staged.credentials,
            _credential_text(api_url, token, queue_key, paths.queue, workspace_id),
            0o600,
        )
        _write_private(staged.adapter, _adapter_text(paths.credentials), 0o700)
        marker = {
            "api_url": api_url,
            "cursor_version": ".".join(str(value) for value in version),
            "hook_fidelity": target.hooks["fidelity"],
            "hooks_config": str(paths.hooks_config),
            "rule_config": str(paths.rule_config),
            "host": "cursor",
            "host_target_schema": target.schema_version,
            "host_target_version": target.descriptor_version,
            "owner": PLUGIN_ID,
        }
        _write_private(staged.marker, json.dumps(marker, sort_keys=True) + "\n", 0o600)
        for filename in ("capture.db", "capture.cursor-state.db"):
            existing = paths.root / filename
            if existing.exists():
                if existing.is_symlink() or not existing.is_file():
                    raise InstallError("Cursor state path is unsafe")
                shutil.copy2(existing, staging / filename)
                os.chmod(staging / filename, 0o600)
        if paths.root.exists():
            paths.root.rename(backup)
        staging.rename(paths.root)
        swapped = True
        _atomic_write(paths.hooks_config, json.dumps(merged_hooks, indent=2, sort_keys=True) + "\n")
        hooks_changed = True
        _atomic_write(paths.rule_config, rule_text)
        rule_changed = True
        _detect_cursor(runner)
        check(api_url, token, queue_key, workspace_id)
    except Exception:
        if rule_changed:
            if original_rule is None:
                paths.rule_config.unlink(missing_ok=True)
            else:
                _atomic_write(paths.rule_config, original_rule.decode("utf-8"))
        if hooks_changed:
            if original_config is None:
                paths.hooks_config.unlink(missing_ok=True)
            else:
                _atomic_write(paths.hooks_config, original_config.decode("utf-8"))
        if backup.exists():
            if paths.root.exists():
                shutil.rmtree(paths.root)
            backup.rename(paths.root)
        elif swapped and paths.root.exists():
            shutil.rmtree(paths.root)
        if staging.exists():
            shutil.rmtree(staging)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)


def doctor_cursor(
    paths: CursorInstallPaths,
    *,
    smoke: bool = False,
    runner: Runner = _run,
    brain_check: Callable[[str, str, str, str | None], None] | None = None,
) -> None:
    version = _detect_cursor(runner)
    try:
        target = negotiate_host_target("cursor", version, {"capture", "recall"})
        assert_owned_install_root(paths.root, expected_marker=target.ownership["marker"])
    except HostTargetError as exc:
        raise InstallError(str(exc)) from None
    marker, values = _load_install(paths)
    if marker.get("hooks_config") != str(paths.hooks_config):
        raise InstallError("Cursor hook ownership marker does not match this configuration")
    if marker.get("rule_config") != str(paths.rule_config):
        raise InstallError("Cursor rule ownership marker does not match this configuration")
    commands = _owned_commands(_load_hooks(paths.hooks_config))
    if len(commands) != 6 or set(commands) != {str(paths.adapter)}:
        raise InstallError("Cursor hooks are missing or modified")
    packaged_rule = Path(__file__).with_name("cursor_rules") / "albert-memory.mdc"
    if (
        paths.rule_config.is_symlink()
        or paths.rule_config.read_bytes() != packaged_rule.read_bytes()
    ):
        raise InstallError("Cursor rule is missing or modified")
    check = brain_check or (
        lambda url, secret, key, workspace: _brain_check(
            url, secret, key, workspace, smoke=smoke, event_classes=CURSOR_EVENTS
        )
    )
    check(
        values["ALBERT_API_URL"],
        values["ALBERT_CAPTURE_TOKEN"],
        values["ALBERT_CAPTURE_QUEUE_KEY"],
        values.get("ALBERT_WORKSPACE_ID"),
    )


def revoke_cursor(paths: CursorInstallPaths) -> None:
    try:
        target = negotiate_host_target("cursor", (2026, 10, 22), {"revoke"})
        assert_owned_install_root(paths.root, expected_marker=target.ownership["marker"])
    except HostTargetError as exc:
        raise InstallError(str(exc)) from None
    marker, _ = _load_install(paths)
    paths.credentials.unlink()
    marker["revoked"] = True
    _write_private(paths.marker, json.dumps(marker, sort_keys=True) + "\n", 0o600)


def uninstall_cursor(paths: CursorInstallPaths, *, runner: Runner = _run) -> None:
    version = _detect_cursor(runner)
    try:
        target = negotiate_host_target("cursor", version, {"uninstall"})
        assert_owned_install_root(paths.root, expected_marker=target.ownership["marker"])
    except HostTargetError as exc:
        raise InstallError(str(exc)) from None
    marker, _ = _load_install(paths, require_credentials=False)
    if marker.get("hooks_config") != str(paths.hooks_config):
        raise InstallError("Cursor hook ownership marker does not match this configuration")
    if marker.get("rule_config") != str(paths.rule_config):
        raise InstallError("Cursor rule ownership marker does not match this configuration")
    packaged_rule = Path(__file__).with_name("cursor_rules") / "albert-memory.mdc"
    if (
        paths.rule_config.is_symlink()
        or paths.rule_config.read_bytes() != packaged_rule.read_bytes()
    ):
        raise InstallError("Cursor rule is foreign or modified")
    updated = _remove_owned_hooks(_load_hooks(paths.hooks_config), paths.adapter)
    original_config = paths.hooks_config.read_bytes()
    tombstone = paths.root.with_name(f".{paths.root.name}.uninstall-{uuid.uuid4().hex}")
    paths.root.rename(tombstone)
    try:
        _atomic_write(paths.hooks_config, json.dumps(updated, indent=2, sort_keys=True) + "\n")
        paths.rule_config.unlink()
        shutil.rmtree(tombstone)
    except Exception:
        _atomic_write(paths.hooks_config, original_config.decode("utf-8"))
        if not paths.rule_config.exists():
            _atomic_write(paths.rule_config, packaged_rule.read_text(encoding="utf-8"))
        if tombstone.exists():
            tombstone.rename(paths.root)
        raise


def _default_paths(project: Path | None = None) -> CursorInstallPaths:
    cursor_home = Path(os.environ.get("CURSOR_HOME", "~/.cursor")).expanduser()
    if project is None:
        return CursorInstallPaths(
            cursor_home / "albert",
            cursor_home / "hooks.json",
            cursor_home / "rules" / "albert-memory.mdc",
        )
    project_root = project.expanduser().resolve()
    project_key = hashlib.sha256(str(project_root).encode()).hexdigest()[:16]
    return CursorInstallPaths(
        cursor_home / "albert" / "projects" / project_key,
        project_root / ".cursor" / "hooks.json",
        project_root / ".cursor" / "rules" / "albert-memory.mdc",
    )


@app.command("install")
def install_command(
    api_url: str = typer.Option(..., "--api-url"),
    workspace_id: str | None = typer.Option(None, "--workspace-id"),
    token_stdin: bool = typer.Option(False, "--token-stdin"),
    token_fd: int | None = typer.Option(None, "--token-fd", min=0),
    project: Path | None = typer.Option(None, "--project", file_okay=False),
) -> None:
    try:
        if token_stdin and token_fd is not None:
            raise InstallError("choose only one private token input mode")
        if token_fd is not None:
            with os.fdopen(os.dup(token_fd), encoding="utf-8") as stream:
                token = stream.read(8193)
        elif token_stdin:
            token = sys.stdin.read(8193)
        else:
            token = getpass.getpass("Albert capture token: ")
        if len(token) > 8192:
            raise InstallError("credential is too long")
        install_cursor(
            api_url=api_url,
            token=token.strip(),
            workspace_id=workspace_id,
            paths=_default_paths(project),
        )
        typer.echo("Albert Memory installed. Review and trust the new Cursor hooks normally.")
    except (InstallError, OSError) as exc:
        typer.echo(f"Install failed safely: {exc}", err=True)
        raise typer.Exit(1) from None


@app.command("doctor")
def doctor_command(
    smoke: bool = typer.Option(False, "--smoke"),
    project: Path | None = typer.Option(None, "--project", file_okay=False),
) -> None:
    try:
        doctor_cursor(_default_paths(project), smoke=smoke)
        typer.echo("Albert Memory Cursor doctor passed.")
    except (InstallError, OSError) as exc:
        typer.echo(f"Doctor failed: {exc}", err=True)
        raise typer.Exit(1) from None


@app.command("upgrade")
def upgrade_command(
    api_url: str = typer.Option(..., "--api-url"),
    workspace_id: str | None = typer.Option(None, "--workspace-id"),
    token_stdin: bool = typer.Option(False, "--token-stdin"),
    token_fd: int | None = typer.Option(None, "--token-fd", min=0),
    project: Path | None = typer.Option(None, "--project", file_okay=False),
) -> None:
    """Atomically replace the target while preserving its encrypted queue."""
    install_command(api_url, workspace_id, token_stdin, token_fd, project)


@app.command("rotate")
def rotate_command(
    api_url: str = typer.Option(..., "--api-url"),
    workspace_id: str | None = typer.Option(None, "--workspace-id"),
    token_stdin: bool = typer.Option(False, "--token-stdin"),
    token_fd: int | None = typer.Option(None, "--token-fd", min=0),
    project: Path | None = typer.Option(None, "--project", file_okay=False),
) -> None:
    """Reconnect with a replacement credential and preserve state on failure."""
    install_command(api_url, workspace_id, token_stdin, token_fd, project)


@app.command("revoke")
def revoke_command(
    project: Path | None = typer.Option(None, "--project", file_okay=False),
) -> None:
    try:
        revoke_cursor(_default_paths(project))
        typer.echo("Albert credential removed; hooks and unrelated Cursor settings remain.")
    except (InstallError, OSError) as exc:
        typer.echo(f"Revoke failed safely: {exc}", err=True)
        raise typer.Exit(1) from None


@app.command("uninstall")
def uninstall_command(
    project: Path | None = typer.Option(None, "--project", file_okay=False),
) -> None:
    try:
        uninstall_cursor(_default_paths(project))
        typer.echo("Albert-owned Cursor files and hook entries removed; unrelated settings remain.")
    except (InstallError, OSError) as exc:
        typer.echo(f"Uninstall failed safely: {exc}", err=True)
        raise typer.Exit(1) from None


if __name__ == "__main__":
    app()