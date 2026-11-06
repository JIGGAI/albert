"""Transactional lifecycle management for the supported Codex hook target."""

from __future__ import annotations

import getpass
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
from albert.codex_adapter import CODEX_EVENTS, SUPPORTED_HOOKS
from albert.host_targets import (
    HostTargetError,
    assert_owned_install_root,
    load_host_target,
    negotiate_host_target,
)

app = typer.Typer(help="Securely install and operate Albert for Codex.")
PLUGIN_ID = "albert-memory"
VERSION_PATTERN = re.compile(r"(?:codex-cli\s+|codex\s+)?v?(\d+)\.(\d+)\.(\d+)", re.I)
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]

# Seconds Codex allows each hook. Recall hooks search (and may embed) on the Brain,
# so they get longer than the capture-only hooks.
HOOK_TIMEOUTS = {"SessionStart": 5, "UserPromptSubmit": 5}


@dataclass(frozen=True)
class CodexInstallPaths:
    root: Path
    hooks_config: Path

    @property
    def credentials(self) -> Path:
        return self.root / "credentials.json"

    @property
    def marker(self) -> Path:
        return self.root / "install.json"

    @property
    def adapter(self) -> Path:
        return self.root / "bin" / "albert-codex-adapter"

    @property
    def queue(self) -> Path:
        return self.root / "capture.db"


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _detect_codex(runner: Runner) -> tuple[int, int, int]:
    result = runner(["codex", "--version"])
    if result.returncode:
        raise InstallError("Codex detection failed")
    match = VERSION_PATTERN.search(result.stdout)
    if not match:
        raise InstallError("unsupported Codex version output")
    version = tuple(int(value) for value in match.groups())
    try:
        negotiate_host_target("codex", version, {"capture", "recall"})
    except HostTargetError as exc:
        raise InstallError(str(exc)) from None
    return version


def _write_private(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, mode)


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    if path.is_symlink():
        raise InstallError("Codex hooks configuration must not be a symlink")
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
        "ALBERT_CAPTURE_SCOPE": "codex:default",
        "ALBERT_CAPTURE_TOKEN": token,
    }
    if workspace_id:
        values["ALBERT_WORKSPACE_ID"] = workspace_id
    return json.dumps(values, sort_keys=True) + "\n"


def _adapter_text(credentials: Path) -> str:
    return (
        "#!/bin/sh\n"
        f"export ALBERT_CAPTURE_CREDENTIAL_FILE={shlex.quote(str(credentials))}\n"
        f"exec {shlex.quote(sys.executable)} -m albert.codex_adapter\n"
    )


def _load_install(
    paths: CodexInstallPaths, *, require_credentials: bool = True
) -> tuple[dict, dict]:
    if not paths.marker.exists() or (require_credentials and not paths.credentials.exists()):
        raise InstallError("no Albert-owned Codex installation found")
    try:
        marker = json.loads(paths.marker.read_text(encoding="utf-8"))
        values = (
            json.loads(paths.credentials.read_text(encoding="utf-8"))
            if paths.credentials.exists()
            else {}
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError("Codex installation metadata is invalid") from exc
    if marker.get("owner") != PLUGIN_ID or marker.get("host") != "codex":
        raise InstallError("installation ownership marker is invalid")
    if not isinstance(values, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in values.items()
    ):
        raise InstallError("credential file is invalid")
    return marker, values


def _load_hooks(path: Path) -> dict:
    if path.is_symlink():
        raise InstallError("Codex hooks configuration must not be a symlink")
    if not path.exists():
        return {"hooks": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError("Codex hooks configuration is invalid") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("hooks"), dict):
        raise InstallError("Codex hooks configuration is malformed")
    return payload


def _owned_commands(payload: dict) -> list[str]:
    commands: list[str] = []
    try:
        for matchers in payload["hooks"].values():
            for matcher in matchers:
                for hook in matcher["hooks"]:
                    command = hook.get("command")
                    if isinstance(command, str) and "albert-codex-adapter" in command:
                        commands.append(command)
    except (AttributeError, KeyError, TypeError) as exc:
        raise InstallError("Codex hooks configuration is malformed") from exc
    return commands


def _configured_hooks(source: Path, adapter: Path) -> dict[str, list[dict]]:
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
        hooks = payload["hooks"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise InstallError("Codex hook fixture is malformed") from exc
    if set(hooks) != SUPPORTED_HOOKS:
        raise InstallError("Codex hook fixture contains unsupported events")
    for event, matchers in hooks.items():
        if not isinstance(matchers, list) or len(matchers) != 1:
            raise InstallError("Codex hook fixture is untrusted")
        for matcher in matchers:
            if set(matcher) != {"hooks"} or len(matcher["hooks"]) != 1:
                raise InstallError("Codex hook fixture is untrusted")
            hook = matcher["hooks"][0]
            allowed = {"command", "statusMessage", "timeout", "type"}
            if set(hook) - allowed or hook.get("command") != "albert-codex-adapter":
                raise InstallError("Codex hook fixture is untrusted")
            if hook.get("type") != "command" or hook.get("timeout") != HOOK_TIMEOUTS.get(event, 2):
                raise InstallError("Codex hook fixture is untrusted")
            hook["command"] = str(adapter)
    return hooks


def _merge_hooks(current: dict, owned: dict[str, list[dict]], adapter: Path) -> dict:
    expected = str(adapter)
    existing = _owned_commands(current)
    if existing and any(command != expected for command in existing):
        raise InstallError("Codex hooks contain a foreign or stale Albert entry")
    merged = json.loads(json.dumps(current))
    for event, matchers in owned.items():
        entries = merged["hooks"].setdefault(event, [])
        entries[:] = [
            matcher
            for matcher in entries
            if not any(
                hook.get("command") == expected
                for hook in matcher.get("hooks", [])
                if isinstance(hook, dict)
            )
        ]
        entries.extend(matchers)
    return merged


def _remove_owned_hooks(current: dict, adapter: Path) -> dict:
    expected = str(adapter)
    existing = _owned_commands(current)
    if any(command != expected for command in existing):
        raise InstallError("Codex hooks contain a foreign or modified Albert entry")
    updated = json.loads(json.dumps(current))
    for event, entries in list(updated["hooks"].items()):
        kept = [
            matcher
            for matcher in entries
            if not any(
                hook.get("command") == expected
                for hook in matcher.get("hooks", [])
                if isinstance(hook, dict)
            )
        ]
        if kept:
            updated["hooks"][event] = kept
        else:
            del updated["hooks"][event]
    return updated


def install_codex(
    *,
    api_url: str,
    token: str,
    workspace_id: str | None,
    paths: CodexInstallPaths,
    runner: Runner = _run,
    brain_check: Callable[[str, str, str, str | None], None] | None = None,
    hook_source: Path | None = None,
) -> None:
    """Validate, stage, atomically replace, and prove a Codex installation."""
    version = _detect_codex(runner)
    try:
        target = negotiate_host_target(
            "codex", version, {"capture", "recall", "rollback", "uninstall", "upgrade"}
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
            url, secret, key, workspace, smoke=True, event_classes=CODEX_EVENTS
        )
    )
    check(api_url, token, queue_key, workspace_id)

    source = hook_source or Path(__file__).with_name("codex_hooks") / "hooks.json"
    owned_hooks = _configured_hooks(source, paths.adapter)
    merged_hooks = _merge_hooks(current_hooks, owned_hooks, paths.adapter)
    original_config = paths.hooks_config.read_bytes() if paths.hooks_config.exists() else None
    backup = paths.root.with_name(f".{paths.root.name}.rollback-{uuid.uuid4().hex}")
    staging = paths.root.with_name(f".{paths.root.name}.staging-{uuid.uuid4().hex}")
    staged = CodexInstallPaths(staging, paths.hooks_config)
    swapped = False
    config_changed = False
    try:
        _write_private(
            staged.credentials,
            _credential_text(api_url, token, queue_key, paths.queue, workspace_id),
            0o600,
        )
        _write_private(staged.adapter, _adapter_text(paths.credentials), 0o700)
        marker = {
            "api_url": api_url,
            "codex_version": ".".join(str(value) for value in version),
            "hook_fidelity": target.hooks["fidelity"],
            "hooks_config": str(paths.hooks_config),
            "host": "codex",
            "host_target_schema": target.schema_version,
            "host_target_version": target.descriptor_version,
            "owner": PLUGIN_ID,
        }
        _write_private(staged.marker, json.dumps(marker, sort_keys=True) + "\n", 0o600)
        for filename in ("capture.db", "capture.codex-state.db"):
            existing = paths.root / filename
            if existing.exists():
                if existing.is_symlink() or not existing.is_file():
                    raise InstallError("Codex state path is unsafe")
                shutil.copy2(existing, staging / filename)
                os.chmod(staging / filename, 0o600)
        if paths.root.exists():
            paths.root.rename(backup)
        staging.rename(paths.root)
        swapped = True
        _atomic_write(paths.hooks_config, json.dumps(merged_hooks, indent=2, sort_keys=True) + "\n")
        config_changed = True
        _detect_codex(runner)
        check(api_url, token, queue_key, workspace_id)
    except Exception:
        if config_changed:
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


def doctor_codex(
    paths: CodexInstallPaths,
    *,
    smoke: bool = False,
    runner: Runner = _run,
    brain_check: Callable[[str, str, str, str | None], None] | None = None,
) -> None:
    version = _detect_codex(runner)
    try:
        target = negotiate_host_target("codex", version, {"capture", "recall"})
        assert_owned_install_root(paths.root, expected_marker=target.ownership["marker"])
    except HostTargetError as exc:
        raise InstallError(str(exc)) from None
    marker, values = _load_install(paths)
    if marker.get("hooks_config") != str(paths.hooks_config):
        raise InstallError("Codex hook ownership marker does not match this configuration")
    commands = _owned_commands(_load_hooks(paths.hooks_config))
    if len(commands) != len(SUPPORTED_HOOKS) or set(commands) != {str(paths.adapter)}:
        raise InstallError("Codex hooks are missing or modified")
    check = brain_check or (
        lambda url, secret, key, workspace: _brain_check(
            url, secret, key, workspace, smoke=smoke, event_classes=CODEX_EVENTS
        )
    )
    check(
        values["ALBERT_API_URL"],
        values["ALBERT_CAPTURE_TOKEN"],
        values["ALBERT_CAPTURE_QUEUE_KEY"],
        values.get("ALBERT_WORKSPACE_ID"),
    )


def revoke_codex(paths: CodexInstallPaths) -> None:
    try:
        tested = load_host_target("codex").host["tested"]
        version = tuple(int(part) for part in tested.split("."))
        target = negotiate_host_target("codex", version, {"revoke"})
        assert_owned_install_root(paths.root, expected_marker=target.ownership["marker"])
    except HostTargetError as exc:
        raise InstallError(str(exc)) from None
    marker, _ = _load_install(paths)
    paths.credentials.unlink()
    marker["revoked"] = True
    _write_private(paths.marker, json.dumps(marker, sort_keys=True) + "\n", 0o600)


def uninstall_codex(paths: CodexInstallPaths, *, runner: Runner = _run) -> None:
    version = _detect_codex(runner)
    try:
        target = negotiate_host_target("codex", version, {"uninstall"})
        assert_owned_install_root(paths.root, expected_marker=target.ownership["marker"])
    except HostTargetError as exc:
        raise InstallError(str(exc)) from None
    marker, _ = _load_install(paths, require_credentials=False)
    if marker.get("hooks_config") != str(paths.hooks_config):
        raise InstallError("Codex hook ownership marker does not match this configuration")
    updated = _remove_owned_hooks(_load_hooks(paths.hooks_config), paths.adapter)
    original_config = paths.hooks_config.read_bytes()
    tombstone = paths.root.with_name(f".{paths.root.name}.uninstall-{uuid.uuid4().hex}")
    paths.root.rename(tombstone)
    try:
        _atomic_write(paths.hooks_config, json.dumps(updated, indent=2, sort_keys=True) + "\n")
        shutil.rmtree(tombstone)
    except Exception:
        _atomic_write(paths.hooks_config, original_config.decode("utf-8"))
        if tombstone.exists():
            tombstone.rename(paths.root)
        raise


def _default_paths() -> CodexInstallPaths:
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    return CodexInstallPaths(codex_home / "albert", codex_home / "hooks.json")


@app.command("install")
def install_command(
    api_url: str = typer.Option(..., "--api-url"),
    workspace_id: str | None = typer.Option(None, "--workspace-id"),
    token_stdin: bool = typer.Option(False, "--token-stdin"),
    token_fd: int | None = typer.Option(None, "--token-fd", min=0),
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
        install_codex(
            api_url=api_url,
            token=token.strip(),
            workspace_id=workspace_id,
            paths=_default_paths(),
        )
        typer.echo("Albert Memory installed. Review and trust the new Codex hooks normally.")
    except (InstallError, OSError) as exc:
        typer.echo(f"Install failed safely: {exc}", err=True)
        raise typer.Exit(1) from None


@app.command("doctor")
def doctor_command(smoke: bool = typer.Option(False, "--smoke")) -> None:
    try:
        doctor_codex(_default_paths(), smoke=smoke)
        typer.echo("Albert Memory Codex doctor passed.")
    except (InstallError, OSError) as exc:
        typer.echo(f"Doctor failed: {exc}", err=True)
        raise typer.Exit(1) from None


@app.command("upgrade")
def upgrade_command(
    api_url: str = typer.Option(..., "--api-url"),
    workspace_id: str | None = typer.Option(None, "--workspace-id"),
    token_stdin: bool = typer.Option(False, "--token-stdin"),
    token_fd: int | None = typer.Option(None, "--token-fd", min=0),
) -> None:
    """Atomically replace the target while preserving its encrypted queue."""
    install_command(api_url, workspace_id, token_stdin, token_fd)


@app.command("rotate")
def rotate_command(
    api_url: str = typer.Option(..., "--api-url"),
    workspace_id: str | None = typer.Option(None, "--workspace-id"),
    token_stdin: bool = typer.Option(False, "--token-stdin"),
    token_fd: int | None = typer.Option(None, "--token-fd", min=0),
) -> None:
    """Reconnect with a replacement credential and preserve state on failure."""
    install_command(api_url, workspace_id, token_stdin, token_fd)


@app.command("revoke")
def revoke_command() -> None:
    try:
        revoke_codex(_default_paths())
        typer.echo("Albert credential removed; hooks and unrelated Codex settings remain.")
    except (InstallError, OSError) as exc:
        typer.echo(f"Revoke failed safely: {exc}", err=True)
        raise typer.Exit(1) from None


@app.command("uninstall")
def uninstall_command() -> None:
    try:
        uninstall_codex(_default_paths())
        typer.echo("Albert-owned Codex files and hook entries removed; unrelated settings remain.")
    except (InstallError, OSError) as exc:
        typer.echo(f"Uninstall failed safely: {exc}", err=True)
        raise typer.Exit(1) from None


if __name__ == "__main__":
    app()
