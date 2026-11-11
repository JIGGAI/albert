"""Transactional lifecycle management for the supported Claude Code plugin target."""

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

import httpx
import typer

from albert.capture_installer import (
    InstallError,
    _brain_check,
    _package_version,
    _validate_secret,
    read_private_link,
    redeem_cloud_link,
)
from albert.capture_sdk import generate_queue_key
from albert.claude_adapter import CLAUDE_EVENTS
from albert.host_targets import HostTargetError, assert_owned_install_root, negotiate_host_target

app = typer.Typer(help="Securely install and operate Albert for Claude Code.")
PLUGIN_ID = "albert-memory"
# The installed plugin is published to Claude Code through a local marketplace
# inside the owned tree, so plain `claude` loads it and its hooks always match
# the installed albert-memory version. People who installed from the public
# marketplace (`albert-memory@albert`) keep that one instead.
LOCAL_MARKETPLACE = "albert-local"
LOCAL_PLUGIN = f"{PLUGIN_ID}@{LOCAL_MARKETPLACE}"
VERSION_PATTERN = re.compile(r"(?:Claude Code\s+)?v?(\d+)\.(\d+)\.(\d+)")
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


# Seconds Claude Code allows each hook. Recall hooks search (and may embed) on the
# Brain, so they get longer than the capture-only hooks.
HOOK_TIMEOUTS = {"SessionStart": 5, "UserPromptSubmit": 5}


@dataclass(frozen=True)
class ClaudeInstallPaths:
    root: Path

    @property
    def credentials(self) -> Path:
        return self.root / "credentials.env"

    @property
    def marker(self) -> Path:
        return self.root / "install.json"

    @property
    def marketplace(self) -> Path:
        return self.root / "marketplace"

    @property
    def plugin(self) -> Path:
        return self.marketplace / "plugin"

    @property
    def adapter(self) -> Path:
        return self.root / "bin" / "albert-claude-adapter"

    @property
    def queue(self) -> Path:
        return self.root / "capture.db"


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _checked(runner: Runner, command: list[str], label: str) -> subprocess.CompletedProcess[str]:
    result = runner(command)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise InstallError(f"{label} failed{suffix}")
    return result


def _detect_claude(runner: Runner) -> tuple[int, int, int]:
    result = _checked(runner, ["claude", "--version"], "Claude Code detection")
    match = VERSION_PATTERN.search(result.stdout)
    if not match:
        raise InstallError("unsupported Claude Code version output")
    version = tuple(int(value) for value in match.groups())
    try:
        negotiate_host_target("claude-code", version, {"capture", "recall"})
    except HostTargetError as exc:
        raise InstallError(str(exc)) from None
    return version


def _write_private(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, mode)


def _credential_text(
    api_url: str, token: str, queue_key: str, queue_path: Path, workspace_id: str | None
) -> str:
    values = {
        "ALBERT_API_URL": api_url.rstrip("/"),
        "ALBERT_CAPTURE_QUEUE_KEY": queue_key,
        "ALBERT_CAPTURE_QUEUE_PATH": str(queue_path),
        "ALBERT_CAPTURE_SCOPE": "claude-code:default",
        "ALBERT_CAPTURE_TOKEN": token,
    }
    if workspace_id:
        values["ALBERT_WORKSPACE_ID"] = workspace_id
    return json.dumps(values, sort_keys=True) + "\n"


def _adapter_text(credentials: Path) -> str:
    return (
        "#!/bin/sh\n"
        f"export ALBERT_CAPTURE_CREDENTIAL_FILE={shlex.quote(str(credentials))}\n"
        f"exec {shlex.quote(sys.executable)} -m albert.claude_adapter\n"
    )


def _marketplace_text() -> str:
    return (
        json.dumps(
            {
                "name": LOCAL_MARKETPLACE,
                "owner": {"name": "JIGGAI"},
                "plugins": [
                    {
                        "description": "Albert automatic memory for Claude Code",
                        "name": PLUGIN_ID,
                        "source": "./plugin",
                    }
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _installed_plugin_ids(runner: Runner) -> set[str]:
    result = _checked(runner, ["claude", "plugin", "list", "--json"], "Claude plugin listing")
    try:
        items = json.loads(result.stdout)
    except ValueError:
        raise InstallError("Claude plugin listing is malformed") from None
    if not isinstance(items, list):
        raise InstallError("Claude plugin listing is malformed")
    return {
        item["id"] for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _register_plugin(paths: ClaudeInstallPaths, runner: Runner) -> bool:
    """Make plain `claude` load the plugin; True when this call newly registered it.

    The local plugin loads in place from the owned tree, so an upgrade only swaps
    files; `plugin update` re-records the version. All three commands are
    idempotent.
    """
    installed = _installed_plugin_ids(runner)
    if any(item.startswith(f"{PLUGIN_ID}@") and item != LOCAL_PLUGIN for item in installed):
        # Installed from the public marketplace: its hooks read the same
        # credential file, and a second copy would capture every event twice.
        return False
    fresh = LOCAL_PLUGIN not in installed
    try:
        _checked(
            runner,
            ["claude", "plugin", "marketplace", "add", str(paths.marketplace)],
            "Claude marketplace registration",
        )
        _checked(runner, ["claude", "plugin", "install", LOCAL_PLUGIN], "Claude plugin install")
        _checked(runner, ["claude", "plugin", "update", LOCAL_PLUGIN], "Claude plugin update")
    except InstallError:
        if fresh:
            _unregister_plugin(runner)
        raise
    return fresh


def _unregister_plugin(runner: Runner) -> None:
    if LOCAL_PLUGIN in _installed_plugin_ids(runner):
        _checked(runner, ["claude", "plugin", "uninstall", LOCAL_PLUGIN], "Claude plugin removal")
    runner(["claude", "plugin", "marketplace", "remove", LOCAL_MARKETPLACE])


def _load_install(
    paths: ClaudeInstallPaths, *, require_credentials: bool = True
) -> tuple[dict, dict]:
    if not paths.marker.exists() or (require_credentials and not paths.credentials.exists()):
        raise InstallError("no Albert-owned Claude Code installation found")
    try:
        marker = json.loads(paths.marker.read_text(encoding="utf-8"))
        values = (
            json.loads(paths.credentials.read_text(encoding="utf-8"))
            if paths.credentials.exists()
            else {}
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError("Claude Code installation metadata is invalid") from exc
    if marker.get("owner") != PLUGIN_ID or marker.get("host") != "claude-code":
        raise InstallError("installation ownership marker is invalid")
    if not isinstance(values, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in values.items()
    ):
        raise InstallError("credential file is invalid")
    return marker, values


def _stage_plugin(staged: ClaudeInstallPaths, source: Path, adapter_command: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise InstallError("Claude plugin source must be a regular directory")
    if any(path.is_symlink() for path in source.rglob("*")):
        raise InstallError("Claude plugin source must not contain symlinks")
    shutil.copytree(source, staged.plugin, symlinks=False)
    hooks_path = staged.plugin / "hooks" / "hooks.json"
    try:
        hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        entries = hooks["hooks"]
        if set(entries) != {
            "PreCompact",
            "SessionEnd",
            "SessionStart",
            "Stop",
            "StopFailure",
            "UserPromptSubmit",
        }:
            raise InstallError("Claude plugin hook manifest contains unsupported events")
        for event, matchers in entries.items():
            if not isinstance(matchers, list) or len(matchers) != 1:
                raise InstallError("Claude plugin hook manifest is untrusted")
            for matcher in matchers:
                if set(matcher) != {"hooks"} or len(matcher["hooks"]) != 1:
                    raise InstallError("Claude plugin hook manifest is untrusted")
                for hook in matcher["hooks"]:
                    if hook != {
                        "type": "command",
                        "command": "albert-claude-adapter",
                        "timeout": HOOK_TIMEOUTS.get(event, 2),
                    }:
                        raise InstallError("Claude plugin hook manifest is untrusted")
                    hook["command"] = str(adapter_command)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise InstallError("Claude plugin hook manifest is malformed") from exc
    hooks_path.write_text(json.dumps(hooks, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # Run the MCP server from the same installation as the hooks, not whatever
    # `albert-mcp` happens to be first on PATH.
    mcp_path = staged.plugin / ".mcp.json"
    server = Path(sys.executable).with_name("albert-mcp")
    if mcp_path.exists() and server.exists():
        try:
            mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
            if mcp["mcpServers"]["albert"]["command"] != "albert-mcp":
                raise InstallError("Claude plugin MCP manifest is untrusted")
            mcp["mcpServers"]["albert"]["command"] = str(server)
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise InstallError("Claude plugin MCP manifest is malformed") from exc
        mcp_path.write_text(json.dumps(mcp, indent=2) + "\n", encoding="utf-8")


def install_claude(
    *,
    api_url: str,
    token: str,
    workspace_id: str | None,
    paths: ClaudeInstallPaths,
    runner: Runner = _run,
    brain_check: Callable[[str, str, str, str | None], None] | None = None,
    cloud_metadata: dict[str, object] | None = None,
    plugin_source: Path | None = None,
) -> None:
    """Validate, stage, atomically replace, and prove a Claude Code installation."""
    version = _detect_claude(runner)
    try:
        target = negotiate_host_target(
            "claude-code",
            version,
            {"capture", "recall", "reload", "rollback", "uninstall", "upgrade"},
        )
        assert_owned_install_root(paths.root, expected_marker=target.ownership["marker"])
    except HostTargetError as exc:
        raise InstallError(str(exc)) from None
    token = _validate_secret(token)
    api_url = api_url.rstrip("/")
    if not api_url.startswith(("http://", "https://")):
        raise InstallError("Brain URL must use http:// or https://")
    previous_marker: dict = {}
    previous_credentials: dict = {}
    if paths.root.exists():
        previous_marker, previous_credentials = _load_install(paths, require_credentials=False)
        if not previous_credentials and not previous_marker.get("revoked"):
            raise InstallError("Claude Code installation credentials are missing")
    queue_key = previous_credentials.get("ALBERT_CAPTURE_QUEUE_KEY")
    if not queue_key and previous_marker.get("revoked"):
        queue_key = previous_marker.get("reconnect_queue_key")
        if not isinstance(queue_key, str) or not queue_key:
            raise InstallError("revoked Claude Code installation reconnect state is invalid")
    queue_key = queue_key or generate_queue_key()
    check = brain_check or (
        lambda url, secret, key, workspace: _brain_check(
            url, secret, key, workspace, smoke=True, event_classes=CLAUDE_EVENTS
        )
    )
    check(api_url, token, queue_key, workspace_id)

    source = plugin_source or Path(__file__).with_name("claude_plugin")
    backup = paths.root.with_name(f".{paths.root.name}.rollback-{uuid.uuid4().hex}")
    staging = paths.root.with_name(f".{paths.root.name}.staging-{uuid.uuid4().hex}")
    staged = ClaudeInstallPaths(staging)
    swapped = False
    registered = False
    try:
        _write_private(
            staged.credentials,
            _credential_text(api_url, token, queue_key, paths.queue, workspace_id),
            0o600,
        )
        _write_private(staged.adapter, _adapter_text(paths.credentials), 0o700)
        _stage_plugin(staged, source, paths.adapter)
        _write_private(
            staged.marketplace / ".claude-plugin" / "marketplace.json", _marketplace_text(), 0o644
        )
        marker = {
            "api_url": api_url,
            "claude_version": ".".join(str(value) for value in version),
            "hook_fidelity": target.hooks["fidelity"],
            "host": "claude-code",
            "host_target_schema": target.schema_version,
            "host_target_version": target.descriptor_version,
            "owner": PLUGIN_ID,
        }
        if cloud_metadata is not None:
            marker["cloud"] = cloud_metadata
        _write_private(staged.marker, json.dumps(marker, sort_keys=True) + "\n", 0o600)
        for filename in ("capture.db", "capture.claude-state.db"):
            existing = paths.root / filename
            if existing.exists():
                if existing.is_symlink() or not existing.is_file():
                    raise InstallError("Claude state path is unsafe")
                shutil.copy2(existing, staging / filename)
                os.chmod(staging / filename, 0o600)
        _checked(
            runner,
            ["claude", "plugin", "validate", "--strict", str(staged.plugin)],
            "Claude plugin validation",
        )
        if paths.root.exists():
            paths.root.rename(backup)
        staging.rename(paths.root)
        swapped = True
        _checked(
            runner,
            ["claude", "plugin", "validate", "--strict", str(paths.plugin)],
            "Claude installed plugin validation",
        )
        registered = _register_plugin(paths, runner)
        check(api_url, token, queue_key, workspace_id)
    except Exception:
        if registered:
            _unregister_plugin(runner)
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


def connect_claude(
    *,
    link_url: str,
    paths: ClaudeInstallPaths,
    runner: Runner = _run,
    trust: dict[str, object] | None = None,
    package_version: str | None = None,
    http: httpx.Client | None = None,
    brain_check: Callable[[str, str, str, str | None], None] | None = None,
    plugin_source: Path | None = None,
) -> None:
    """Redeem a Cloud one-time link made for Claude Code and install with it."""
    # Detect before redeeming so an unsupported host never consumes the link.
    _detect_claude(runner)
    redeem_cloud_link(
        link_url=link_url,
        family="claude-code",
        agent_name="Claude Code",
        install=lambda rest_url, credential, workspace_id, cloud: install_claude(
            api_url=rest_url,
            token=credential,
            workspace_id=workspace_id,
            paths=paths,
            runner=runner,
            brain_check=brain_check,
            plugin_source=plugin_source,
            cloud_metadata={**cloud, "package_version": package_version or _package_version()},
        ),
        trust=trust,
        package_version=package_version,
        http=http,
    )


def doctor_claude(
    paths: ClaudeInstallPaths,
    *,
    smoke: bool = False,
    runner: Runner = _run,
    brain_check: Callable[[str, str, str, str | None], None] | None = None,
) -> None:
    version = _detect_claude(runner)
    try:
        target = negotiate_host_target("claude-code", version, {"capture", "recall"})
        assert_owned_install_root(paths.root, expected_marker=target.ownership["marker"])
    except HostTargetError as exc:
        raise InstallError(str(exc)) from None
    _, values = _load_install(paths)
    _checked(
        runner,
        ["claude", "plugin", "validate", "--strict", str(paths.plugin)],
        "Claude plugin validation",
    )
    if not any(item.startswith(f"{PLUGIN_ID}@") for item in _installed_plugin_ids(runner)):
        raise InstallError("Albert Memory is not installed in Claude Code; re-run connect")
    check = brain_check or (
        lambda url, secret, key, workspace: _brain_check(
            url,
            secret,
            key,
            workspace,
            smoke=smoke,
            event_classes=CLAUDE_EVENTS,
        )
    )
    check(
        values["ALBERT_API_URL"],
        values["ALBERT_CAPTURE_TOKEN"],
        values["ALBERT_CAPTURE_QUEUE_KEY"],
        values.get("ALBERT_WORKSPACE_ID"),
    )


def revoke_claude(paths: ClaudeInstallPaths) -> None:
    """Remove only the target credential; plugin and unrelated host state remain."""
    try:
        target = negotiate_host_target("claude-code", (2, 1, 312), {"revoke"})
        assert_owned_install_root(paths.root, expected_marker=target.ownership["marker"])
    except HostTargetError as exc:
        raise InstallError(str(exc)) from None
    marker, values = _load_install(paths)
    marker["reconnect_queue_key"] = values["ALBERT_CAPTURE_QUEUE_KEY"]
    marker["revoked"] = True
    _write_private(paths.marker, json.dumps(marker, sort_keys=True) + "\n", 0o600)
    paths.credentials.unlink()


def uninstall_claude(paths: ClaudeInstallPaths, *, runner: Runner = _run) -> None:
    _detect_claude(runner)
    try:
        target = negotiate_host_target("claude-code", (2, 1, 312), {"uninstall"})
        assert_owned_install_root(paths.root, expected_marker=target.ownership["marker"])
    except HostTargetError as exc:
        raise InstallError(str(exc)) from None
    _load_install(paths, require_credentials=False)
    _unregister_plugin(runner)
    shutil.rmtree(paths.root)


def _default_paths() -> ClaudeInstallPaths:
    return ClaudeInstallPaths(Path("~/.claude/albert").expanduser())


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
        install_claude(
            api_url=api_url,
            token=token.strip(),
            workspace_id=workspace_id,
            paths=_default_paths(),
        )
        typer.echo("Albert Memory installed; start a new Claude Code session.")
    except (InstallError, OSError) as exc:
        typer.echo(f"Install failed safely: {exc}", err=True)
        raise typer.Exit(1) from None


@app.command("connect")
def connect_command(
    link_stdin: bool = typer.Option(
        False, "--link-stdin", help="Read the one-time link from private standard input."
    ),
    link_fd: int | None = typer.Option(
        None,
        "--link-fd",
        min=0,
        help="Read the one-time link from an already-open private file descriptor.",
    ),
) -> None:
    """Connect Claude Code with an Albert Cloud one-time link."""
    try:
        connect_claude(link_url=read_private_link(link_stdin, link_fd), paths=_default_paths())
        typer.echo(
            "Albert Memory connected; capture and recall passed. "
            "Start a new Claude Code session to use it."
        )
    except (InstallError, OSError) as exc:
        typer.echo(f"Connect failed safely: {exc}", err=True)
        raise typer.Exit(1) from None


@app.command("doctor")
def doctor_command(smoke: bool = typer.Option(False, "--smoke")) -> None:
    try:
        doctor_claude(_default_paths(), smoke=smoke)
        typer.echo("Albert Memory Claude Code doctor passed.")
    except InstallError as exc:
        typer.echo(f"Doctor failed: {exc}", err=True)
        raise typer.Exit(1) from None


@app.command("revoke")
def revoke_command() -> None:
    try:
        revoke_claude(_default_paths())
        typer.echo("Albert credential removed; plugin and unrelated Claude settings remain.")
    except InstallError as exc:
        typer.echo(f"Revoke failed safely: {exc}", err=True)
        raise typer.Exit(1) from None


@app.command("uninstall")
def uninstall_command() -> None:
    try:
        uninstall_claude(_default_paths())
        typer.echo("Albert Memory removed from Claude Code; unrelated settings remain.")
    except InstallError as exc:
        typer.echo(f"Uninstall failed safely: {exc}", err=True)
        raise typer.Exit(1) from None


if __name__ == "__main__":
    app()
