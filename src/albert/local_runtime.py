"""Secure, no-Docker lifecycle for a single-node Albert runtime."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

import httpx
import typer

from albert import __version__

app = typer.Typer(
    help="Run Albert's complete basic data plane locally without Docker.", no_args_is_help=True
)
SCHEMA = 1
MARKER = ".albert-local"
CONFIG = "config.json"
SECRETS = "credentials.json"
DATABASE = "albert.sqlite3"
STATE = "runtime.json"
BACKUP_MEMBERS = frozenset({"manifest.json", CONFIG, SECRETS, DATABASE})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def default_root() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "albert"


def _fail(message: str, code: int = 2) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code)


def _atomic_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail(f"cannot read {path}: {exc}")
    if not isinstance(value, dict):
        _fail(f"invalid object in {path}")
    return value


def _assert_private(path: Path) -> None:
    metadata = path.lstat()
    if path.is_symlink() or not path.is_file() or metadata.st_uid != os.geteuid():
        _fail(f"refusing unsafe or foreign file: {path}")
    if metadata.st_mode & 0o077:
        _fail(f"private file has unsafe permissions: {path}")


def _owned_root(root: Path, *, required: bool = True) -> Path:
    root = root.expanduser().absolute()
    if not root.exists():
        if required:
            _fail(f"Albert local runtime is not installed at {root}")
        return root
    metadata = root.lstat()
    if root.is_symlink() or not root.is_dir() or metadata.st_uid != os.geteuid():
        _fail(f"refusing unsafe or foreign runtime root: {root}")
    if metadata.st_mode & 0o077:
        _fail(f"runtime root has unsafe permissions: {root}")
    marker = root / MARKER
    _assert_private(marker)
    identity = _read_json(marker)
    if identity.get("schema") != SCHEMA or not identity.get("instance_id"):
        _fail(f"invalid Albert local ownership marker: {marker}")
    return root


def _settings(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    root = _owned_root(root)
    for name in (CONFIG, SECRETS):
        _assert_private(root / name)
    config, private = _read_json(root / CONFIG), _read_json(root / SECRETS)
    if config.get("schema") != SCHEMA or private.get("schema") != SCHEMA:
        _fail("unsupported local runtime configuration schema")
    marker = _read_json(root / MARKER)
    if not _valid_settings(config, private, marker.get("instance_id")):
        _fail("invalid or unsafe local runtime configuration")
    _assert_private(root / DATABASE)
    return config, private


def _valid_settings(
    config: dict[str, Any], private: dict[str, Any], instance_id: object
) -> bool:
    expected_provider = "sentence-transformers" if config.get("mode") == "semantic" else "hashing"
    return (
        isinstance(instance_id, str)
        and bool(instance_id)
        and config.get("instance_id") == instance_id
        and config.get("mode") in {"basic", "semantic"}
        and config.get("bind_host") == "127.0.0.1"
        and config.get("embedding_provider") == expected_provider
        and isinstance(config.get("api_port"), int)
        and isinstance(config.get("mcp_port"), int)
        and 1024 <= config["api_port"] <= 65535
        and 1024 <= config["mcp_port"] <= 65535
        and config["api_port"] != config["mcp_port"]
        and isinstance(private.get("api_key"), str)
        and private["api_key"].startswith("alb_")
        and isinstance(private.get("api_key_pepper"), str)
        and len(private["api_key_pepper"]) >= 32
    )


def _environment(root: Path) -> dict[str, str]:
    config, private = _settings(root)
    values = {
        "ALBERT_DATABASE_URL": f"sqlite:///{root / DATABASE}",
        "ALBERT_API_KEY_PEPPER": str(private["api_key_pepper"]),
        "ALBERT_PUBLIC_BASE_URL": f"http://127.0.0.1:{config['api_port']}",
        "ALBERT_PUBLIC_MCP_URL": f"http://127.0.0.1:{config['mcp_port']}/mcp",
        "ALBERT_EMBEDDING_PROVIDER": str(config["embedding_provider"]),
        "ALBERT_EMBEDDING_DIMENSIONS": str(config["embedding_dimensions"]),
        "ALBERT_MCP_API_KEY": str(private["api_key"]),
        "ALBERT_API_URL": f"http://127.0.0.1:{config['api_port']}",
    }
    return {**os.environ, **values}


def _preflight(mode: str, api_port: int, mcp_port: int) -> None:
    system, machine = platform.system(), platform.machine().lower()
    if system not in {"Linux", "Darwin"} or machine not in {"x86_64", "amd64", "arm64", "aarch64"}:
        _fail(f"unsupported local runtime host: {system} {machine}")
    if not (1024 <= api_port <= 65535 and 1024 <= mcp_port <= 65535) or api_port == mcp_port:
        _fail("API and MCP ports must be distinct unprivileged ports")
    if mode == "semantic" and importlib.util.find_spec("sentence_transformers") is None:
        _fail("semantic mode requires the 'albert-memory[semantic]' extra")


def _bootstrap_database(root: Path, pepper: str) -> str:
    os.environ["ALBERT_DATABASE_URL"] = f"sqlite:///{root / DATABASE}"
    os.environ["ALBERT_API_KEY_PEPPER"] = pepper
    from albert import models  # noqa: F401
    from albert.db import SessionLocal, initialize_database
    from albert.models import APIKey, Organization, Principal, Workspace
    from albert.security import ALL_CAPABILITIES, issue_api_key

    initialize_database()
    with SessionLocal() as session:
        organization = Organization(name="Local Albert")
        session.add(organization)
        session.flush()
        workspace = Workspace(organization_id=organization.id, name="Default")
        session.add(workspace)
        session.flush()
        principal = Principal(
            organization_id=organization.id,
            workspace_id=workspace.id,
            name="Local administrator",
            principal_type="human",
        )
        session.add(principal)
        session.flush()
        raw, prefix, digest = issue_api_key()
        session.add(
            APIKey(
                principal_id=principal.id,
                prefix=prefix,
                key_hash=digest,
                name="local runtime",
                capabilities=ALL_CAPABILITIES,
            )
        )
        session.commit()
    return raw


def install_runtime(root: Path, mode: str, api_port: int, mcp_port: int) -> bool:
    root = root.expanduser().absolute()
    _preflight(mode, api_port, mcp_port)
    if root.exists():
        config, _private = _settings(root)
        if (
            config.get("mode") != mode
            or config.get("api_port") != api_port
            or config.get("mcp_port") != mcp_port
        ):
            _fail("runtime already exists with different mode or ports; use upgrade")
        if not config.get("installed", True):
            config["installed"] = True
            config["updated_at"] = _now()
            _atomic_json(root / CONFIG, config)
        return False

    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{root.name}-install-", dir=root.parent))
    os.chmod(temporary, 0o700)
    try:
        instance_id = uuid4().hex
        pepper = secrets.token_urlsafe(48)
        _atomic_json(temporary / MARKER, {"schema": SCHEMA, "instance_id": instance_id})
        _atomic_json(
            temporary / CONFIG,
            {
                "schema": SCHEMA,
                "instance_id": instance_id,
                "installed": True,
                "installed_version": __version__,
                "mode": mode,
                "embedding_provider": "sentence-transformers" if mode == "semantic" else "hashing",
                "embedding_dimensions": 384 if mode == "semantic" else 64,
                "bind_host": "127.0.0.1",
                "api_port": api_port,
                "mcp_port": mcp_port,
                "single_node": True,
                "created_at": _now(),
                "updated_at": _now(),
            },
        )
        api_key = _bootstrap_database(temporary, pepper)
        _atomic_json(
            temporary / SECRETS, {"schema": SCHEMA, "api_key_pepper": pepper, "api_key": api_key}
        )
        os.chmod(temporary / DATABASE, 0o600)
        os.replace(temporary, root)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _owned_pid(root: Path, pid: int) -> bool:
    if not _pid_alive(pid):
        return False
    proc = Path(f"/proc/{pid}/cmdline")
    if proc.exists():
        try:
            command = proc.read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            return False
        return "albert.local_runtime" in command and str(root) in command
    try:
        command = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "albert.local_runtime" in command and str(root) in command


def _state(root: Path) -> dict[str, Any] | None:
    path = root / STATE
    if not path.exists():
        return None
    _assert_private(path)
    state = _read_json(path)
    pid = state.get("supervisor_pid")
    return state if isinstance(pid, int) and _owned_pid(root, pid) else None


def _wait_ready(root: Path, timeout: float) -> bool:
    config, _private = _settings(root)
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{config['api_port']}/v1/health/ready"
    while time.monotonic() < deadline:
        if _state(root) is not None:
            try:
                if httpx.get(url, timeout=1).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
        time.sleep(0.1)
    return False


def start_runtime(root: Path, timeout: float = 20) -> None:
    root = _owned_root(root)
    config, _private = _settings(root)
    if not config.get("installed", True):
        _fail("runtime is uninstalled; run install before start")
    if _state(root):
        typer.echo("Albert local runtime is already running")
        return
    for port in (int(config["api_port"]), int(config["mcp_port"])):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                _fail(f"loopback port {port} is already in use")
    logs = root / "logs"
    logs.mkdir(mode=0o700, exist_ok=True)
    log = open(logs / "runtime.log", "ab", buffering=0)
    os.chmod(logs / "runtime.log", 0o600)
    process = subprocess.Popen(
        [sys.executable, "-m", "albert.local_runtime", "supervise", "--root", str(root)],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    log.close()
    if not _wait_ready(root, timeout):
        if process.poll() is None:
            process.terminate()
        _fail(f"runtime did not become ready; inspect {logs / 'runtime.log'}", 1)


def stop_runtime(root: Path, timeout: float = 15) -> bool:
    root = _owned_root(root)
    state = _state(root)
    if state is None:
        (root / STATE).unlink(missing_ok=True)
        return False
    pid = int(state["supervisor_pid"])
    if not _owned_pid(root, pid):
        _fail("refusing to signal a process not owned by this Albert instance")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.1)
    if _pid_alive(pid):
        _fail("runtime did not stop within the timeout", 1)
    (root / STATE).unlink(missing_ok=True)
    return True


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def backup_runtime(root: Path, destination: Path) -> Path:
    root = _owned_root(root)
    config, _private = _settings(root)
    destination = destination.expanduser().absolute()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        _fail(f"backup destination already exists: {destination}")
    descriptor, temporary_name = tempfile.mkstemp(prefix="albert-backup-", suffix=".sqlite3")
    os.close(descriptor)
    temporary_db = Path(temporary_name)
    temporary_zip = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with sqlite3.connect(root / DATABASE) as source, sqlite3.connect(temporary_db) as target:
            source.backup(target)
        members = {
            CONFIG: (root / CONFIG).read_bytes(),
            SECRETS: (root / SECRETS).read_bytes(),
            DATABASE: temporary_db.read_bytes(),
        }
        manifest = {
            "schema": SCHEMA,
            "instance_id": config["instance_id"],
            "created_at": _now(),
            "package_version": __version__,
            "checksums": {name: _sha256(value) for name, value in members.items()},
        }
        with zipfile.ZipFile(temporary_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
            for name, value in members.items():
                archive.writestr(name, value)
        os.chmod(temporary_zip, 0o600)
        os.replace(temporary_zip, destination)
    finally:
        temporary_db.unlink(missing_ok=True)
        temporary_zip.unlink(missing_ok=True)
    return destination


def restore_runtime(root: Path, archive_path: Path) -> None:
    root = _owned_root(root)
    if _state(root):
        _fail("stop the runtime before restoring a backup")
    archive_path = archive_path.expanduser().absolute()
    _assert_private(archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        if frozenset(archive.namelist()) != BACKUP_MEMBERS:
            _fail("backup contains unexpected or missing files")
        manifest = json.loads(archive.read("manifest.json"))
        values = {name: archive.read(name) for name in (CONFIG, SECRETS, DATABASE)}
    if manifest.get("schema") != SCHEMA:
        _fail("unsupported backup schema")
    if any(
        manifest.get("checksums", {}).get(name) != _sha256(value) for name, value in values.items()
    ):
        _fail("backup checksum verification failed")
    restored_config, restored_private = json.loads(values[CONFIG]), json.loads(values[SECRETS])
    if restored_config.get("schema") != SCHEMA or restored_private.get("schema") != SCHEMA:
        _fail("backup configuration is invalid")
    source_instance_id = manifest.get("instance_id")
    if not _valid_settings(restored_config, restored_private, source_instance_id):
        _fail("backup configuration is invalid or unsafe")

    # A restore recovers data and credentials into the already-owned destination.
    # Retaining that destination identity prevents a backup from replacing the
    # ownership boundary while keeping config and marker consistent.
    destination_instance_id = _read_json(root / MARKER)["instance_id"]
    restored_config["instance_id"] = destination_instance_id
    if not _valid_settings(restored_config, restored_private, destination_instance_id):
        _fail("backup configuration is invalid or unsafe")
    values[CONFIG] = (json.dumps(restored_config, sort_keys=True) + "\n").encode()

    staged: dict[str, Path] = {}
    previous: dict[str, Path] = {}
    try:
        for name, value in values.items():
            temporary = root / f".{name}.{uuid4().hex}.restore"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            staged[name] = temporary
        for name, temporary in staged.items():
            rollback = root / f".{name}.{uuid4().hex}.rollback"
            os.replace(root / name, rollback)
            previous[name] = rollback
            os.replace(temporary, root / name)
            os.chmod(root / name, 0o600)
    except BaseException:
        for name, rollback in reversed(previous.items()):
            if rollback.exists():
                os.replace(rollback, root / name)
        raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
        for rollback in previous.values():
            rollback.unlink(missing_ok=True)


@app.command()
def install(
    root: Path = typer.Option(default_root, help="Private Albert data directory."),
    mode: str = typer.Option("basic", help="basic or semantic"),
    api_port: int = typer.Option(8080),
    mcp_port: int = typer.Option(8081),
) -> None:
    """Install a private local runtime and bootstrap its administrator credential."""
    if mode not in {"basic", "semantic"}:
        _fail("mode must be 'basic' or 'semantic'")
    created = install_runtime(root, mode, api_port, mcp_port)
    typer.echo("Albert local runtime installed" if created else "Albert local runtime is installed")


@app.command()
def start(
    root: Path = typer.Option(default_root), timeout: float = typer.Option(20, min=1, max=120)
) -> None:
    """Start API, worker, and MCP on loopback as a background runtime."""
    if not root.expanduser().exists():
        install_runtime(root, "basic", 8080, 8081)
    start_runtime(root, timeout)
    typer.echo("Albert local runtime is ready")


@app.command(hidden=True)
def supervise(root: Path = typer.Option(...)) -> None:
    """Run and supervise the local runtime child processes."""
    root = _owned_root(root)
    config, _private = _settings(root)
    env = _environment(root)
    stop_requested = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    commands = [
        [
            sys.executable,
            "-m",
            "uvicorn",
            "albert.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(config["api_port"]),
        ],
        [sys.executable, "-m", "albert.worker"],
        [
            sys.executable,
            "-m",
            "albert.mcp_server",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(config["mcp_port"]),
        ],
    ]
    children = [subprocess.Popen(command, env=env, close_fds=True) for command in commands]
    _atomic_json(
        root / STATE,
        {
            "schema": SCHEMA,
            "instance_id": config["instance_id"],
            "supervisor_pid": os.getpid(),
            "child_pids": [child.pid for child in children],
            "started_at": _now(),
        },
    )
    result = 0
    try:
        while not stop_requested:
            failed = next((child for child in children if child.poll() is not None), None)
            if failed is not None:
                result = failed.returncode or 1
                break
            time.sleep(0.2)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        (root / STATE).unlink(missing_ok=True)
    raise typer.Exit(result)


@app.command()
def stop(root: Path = typer.Option(default_root)) -> None:
    """Stop this runtime without touching its data or credentials."""
    typer.echo("Albert local runtime stopped" if stop_runtime(root) else "Albert is not running")


@app.command()
def status(
    root: Path = typer.Option(default_root), json_output: bool = typer.Option(False, "--json")
) -> None:
    """Show secret-free install, process, mode, and endpoint status."""
    config, _private = _settings(root)
    state = _state(root)
    payload = {
        "installed": bool(config.get("installed", True)),
        "running": state is not None,
        "ready": bool(state and _wait_ready(root, 1)),
        "version": config["installed_version"],
        "mode": config["mode"],
        "single_node": True,
        "api_url": f"http://127.0.0.1:{config['api_port']}",
        "mcp_url": f"http://127.0.0.1:{config['mcp_port']}/mcp",
    }
    typer.echo(
        json.dumps(payload, sort_keys=True)
        if json_output
        else "\n".join(f"{key}: {value}" for key, value in payload.items())
    )


@app.command()
def backup(destination: Path, root: Path = typer.Option(default_root)) -> None:
    """Create a mode-0600 consistent backup including credentials."""
    typer.echo(str(backup_runtime(root, destination)))


@app.command()
def restore(archive: Path, root: Path = typer.Option(default_root)) -> None:
    """Restore data, configuration, and credentials from a verified backup."""
    restore_runtime(root, archive)
    typer.echo("Albert local backup restored")


@app.command()
def upgrade(
    root: Path = typer.Option(default_root),
    mode: str | None = typer.Option(None, help="Optionally change to basic or semantic mode."),
) -> None:
    """Create a rollback point and adopt the currently installed Albert package."""
    root = _owned_root(root)
    config, _private = _settings(root)
    target_mode = mode or str(config["mode"])
    if target_mode not in {"basic", "semantic"}:
        _fail("mode must be 'basic' or 'semantic'")
    _preflight(target_mode, int(config["api_port"]), int(config["mcp_port"]))
    if _state(root):
        _fail("stop the runtime before upgrading")
    rollback_dir = root / "rollback"
    rollback_dir.mkdir(mode=0o700, exist_ok=True)
    backup_runtime(root, rollback_dir / f"{int(time.time())}-{config['installed_version']}.zip")
    config.update(
        {
            "installed_version": __version__,
            "mode": target_mode,
            "embedding_provider": "sentence-transformers"
            if target_mode == "semantic"
            else "hashing",
            "embedding_dimensions": 384 if target_mode == "semantic" else 64,
            "updated_at": _now(),
        }
    )
    _atomic_json(root / CONFIG, config)
    typer.echo(f"Albert local runtime upgraded to {__version__}; rollback point created")


@app.command()
def rollback(root: Path = typer.Option(default_root)) -> None:
    """Restore the newest local upgrade rollback point."""
    root = _owned_root(root)
    if _state(root):
        _fail("stop the runtime before rollback")
    candidates = sorted((root / "rollback").glob("*.zip"), reverse=True)
    if not candidates:
        _fail("no rollback point is available")
    restore_runtime(root, candidates[0])
    typer.echo(f"Albert local runtime restored from {candidates[0].name}")


@app.command()
def smoke(root: Path = typer.Option(default_root)) -> None:
    """Prove API, MCP, memory, context, and capture-profile parity."""
    config, private = _settings(root)
    if not _state(root):
        _fail("start the runtime before running smoke tests")
    base = f"http://127.0.0.1:{config['api_port']}"
    headers = {"Authorization": f"Bearer {private['api_key']}"}
    sentinel = f"Albert local parity {uuid4().hex}"
    profile_key = f"local-smoke-{uuid4().hex}"
    with httpx.Client(base_url=base, headers=headers, timeout=15) as client:
        responses = {
            "health": client.get("/v1/health/ready"),
            "memory": client.post(
                "/v1/memories", json={"content": sentinel, "sensitivity": "internal"}
            ),
            "search": client.post("/v1/search", json={"query": sentinel, "limit": 5}),
            "context": client.post(
                "/v1/context/session-start",
                json={"query": sentinel, "project_ref": "albert-local-smoke"},
            ),
            "capture_profile": client.post(
                "/v1/capture/profiles",
                json={
                    "profile_key": profile_key,
                    "version": 1,
                    "allowed_event_classes": ["user_goal"],
                },
            ),
        }
    failures = [name for name, response in responses.items() if response.status_code >= 400]
    try:
        with socket.create_connection(("127.0.0.1", int(config["mcp_port"])), timeout=2):
            pass
    except OSError:
        failures.append("mcp")
    if failures:
        _fail(f"parity smoke failed: {', '.join(failures)}", 1)
    typer.echo("Parity smoke passed: API, MCP, memory, context, and capture profile")


@app.command()
def uninstall(
    root: Path = typer.Option(default_root),
    remove_data: bool = typer.Option(
        False, "--remove-data", help="Permanently remove all local data."
    ),
) -> None:
    """Stop Albert and either preserve its private state or remove the owned root."""
    root = _owned_root(root)
    stop_runtime(root)
    if remove_data:
        root = _owned_root(root)
        shutil.rmtree(root)
        typer.echo("Albert local runtime and its data were permanently removed")
        return
    config, _private = _settings(root)
    config["installed"] = False
    config["updated_at"] = _now()
    _atomic_json(root / CONFIG, config)
    typer.echo(f"Albert local runtime uninstalled; data preserved at {root}")


if __name__ == "__main__":
    app()
