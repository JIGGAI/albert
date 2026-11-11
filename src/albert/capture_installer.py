"""Safe installer, doctor, and uninstaller for Albert capture adapters."""

from __future__ import annotations

import getpass
import importlib.metadata
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
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from albert.capture_sdk import CaptureClient, EncryptedQueue, generate_queue_key
from albert.host_targets import (
    HostTargetError,
    assert_owned_install_root,
    load_host_target,
    negotiate_host_target,
)
from albert.openclaw_adapter import OPENCLAW_EVENTS

app = typer.Typer(help="Connect supported agent hosts directly to an Albert Brain.")
PLUGIN_ID = "albert-memory"
MIN_OPENCLAW = (2026, 9, 6)
MAX_OPENCLAW = (2027, 0, 0)
ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,99}$")
TRUST_PATH = Path(__file__).with_name("openclaw_adapter_trust.json")
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


@dataclass(frozen=True)
class CloudManifest:
    capabilities: list[str]
    compensation_url: str
    identity: dict[str, str]
    manifest_id: str
    manifest_version: str
    redemption_url: str
    rest_url: str


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _urlsafe_decode(value: str) -> bytes:
    import base64
    import binascii

    if not re.fullmatch(r"[A-Za-z0-9_-]+", value) or len(value) > 8192:
        raise InstallError("signed adapter metadata is malformed")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise InstallError("signed adapter metadata is malformed") from None


def _https_url(value: object, label: str) -> str:
    from ipaddress import ip_address
    from urllib.parse import urlsplit, urlunsplit

    if not isinstance(value, str) or len(value) > 4096:
        raise InstallError(f"{label} must be a canonical public HTTPS URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise InstallError(f"{label} must be a canonical public HTTPS URL") from None
    hostname = (parsed.hostname or "").rstrip(".").lower()
    try:
        address = ip_address(hostname)
    except ValueError:
        address = None
    private_name = address is None and (
        "." not in hostname or hostname.endswith((".local", ".localhost", ".internal", ".lan"))
    )
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or (address is not None and not address.is_global)
        or private_name
    ):
        raise InstallError(f"{label} must be a canonical public HTTPS URL")
    canonical_host = f"[{hostname}]" if address is not None and address.version == 6 else hostname
    canonical = urlunsplit(
        ("https", canonical_host, parsed.path.rstrip("/") or "/", parsed.query, "")
    )
    if canonical.rstrip("/") != value.rstrip("/"):
        raise InstallError(f"{label} must be a canonical public HTTPS URL")
    return value.rstrip("/")


def _load_trust() -> dict[str, object]:
    try:
        trust = json.loads(TRUST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError("Cloud adapter trust metadata is unavailable") from exc
    if not isinstance(trust, dict):
        raise InstallError("Cloud adapter trust metadata is invalid")
    return trust


def _package_version() -> str:
    try:
        return importlib.metadata.version("albert-memory")
    except importlib.metadata.PackageNotFoundError:
        return "0.4.1"


def _verify_manifest(
    payload: object,
    *,
    link_url: str,
    family: str,
    trust: dict[str, object] | None = None,
    package_version: str | None = None,
) -> CloudManifest:
    """Verify a signed Cloud adapter manifest issued for one host family.

    The family is signed into the manifest, so a link made for OpenClaw cannot
    be redeemed by the Claude Code or Codex installer, or the other way round.
    Host-version support is not part of the manifest: each installer negotiates
    it against its own shipped host target before anything is redeemed.
    """
    from datetime import UTC, datetime, timedelta
    from urllib.parse import urlsplit, urlunsplit

    if not isinstance(payload, dict):
        raise InstallError("Cloud adapter manifest is malformed")
    signature = payload.get("signature")
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    if not isinstance(signature, dict) or set(signature) != {"algorithm", "key_id", "value"}:
        raise InstallError("Cloud adapter manifest signature is malformed")
    key_id = signature.get("key_id")
    loaded_trust = trust if trust is not None else _load_trust()
    keys = loaded_trust.get("manifest_keys")
    if (
        signature.get("algorithm") != "Ed25519"
        or not isinstance(key_id, str)
        or not isinstance(keys, dict)
        or not isinstance(keys.get(key_id), str)
        or not isinstance(signature.get("value"), str)
    ):
        raise InstallError("Cloud adapter manifest is not signed by a trusted key")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(_urlsafe_decode(keys[key_id]))
        public_key.verify(
            _urlsafe_decode(signature["value"]), _canonical_json(unsigned).encode("utf-8")
        )
    except (ValueError, InvalidSignature):
        raise InstallError("Cloud adapter manifest signature is invalid") from None

    if set(unsigned) != {
        "adapter",
        "capabilities",
        "compensation",
        "connection",
        "expires_at",
        "identity",
        "issued_at",
        "manifest_id",
        "manifest_version",
        "redemption",
        "reload",
        "revocation",
        "rollback",
        "schema",
        "verification",
    }:
        raise InstallError("Cloud adapter manifest contains unknown or missing fields")
    version = unsigned.get("manifest_version")
    if not isinstance(version, str) or version.split(".")[0] != "2":
        raise InstallError("unsupported Cloud adapter manifest major version")
    if unsigned.get("schema") != "com.jiggai.albert.adapter-manifest":
        raise InstallError("Cloud adapter manifest schema is unsupported")
    issued_value = unsigned.get("issued_at")
    expires_value = unsigned.get("expires_at")
    if not isinstance(issued_value, str) or not isinstance(expires_value, str):
        raise InstallError("Cloud adapter manifest lifetime is malformed")
    try:
        issued = datetime.fromisoformat(issued_value.replace("Z", "+00:00"))
        expires = datetime.fromisoformat(expires_value.replace("Z", "+00:00"))
    except ValueError:
        raise InstallError("Cloud adapter manifest lifetime is malformed") from None
    now = datetime.now(UTC)
    if (
        issued.utcoffset() != timedelta(0)
        or expires.utcoffset() != timedelta(0)
        or issued > now
        or expires <= now
        or expires - issued > timedelta(hours=1)
    ):
        raise InstallError("Cloud adapter manifest is expired or not yet valid")

    identity = unsigned.get("identity")
    capabilities = unsigned.get("capabilities")
    manifest_id = unsigned.get("manifest_id")
    identity_keys = ("brain_id", "request_id", "workspace_id")
    if (
        not isinstance(identity, dict)
        or set(identity) != {"brain_id", "cloud", "request_id", "workspace_id"}
        or identity.get("cloud") != "albert-cloud"
        or not all(
            isinstance(identity.get(key), str) and ID_PATTERN.fullmatch(identity[key])
            for key in identity_keys
        )
        or identity.get("request_id") != manifest_id
        or not isinstance(capabilities, list)
        or not capabilities
        or capabilities != sorted(set(capabilities))
        or not all(
            isinstance(item, str) and CAPABILITY_PATTERN.fullmatch(item) for item in capabilities
        )
    ):
        raise InstallError("Cloud adapter manifest identity or capabilities are invalid")

    adapter = unsigned.get("adapter")
    if not isinstance(adapter, dict) or set(adapter) != {
        "artifact_sha256",
        "artifact_signature",
        "artifact_url",
        "family",
        "package",
        "package_version",
    }:
        raise InstallError("Cloud adapter package provenance is malformed")
    local_version = package_version or _package_version()
    artifact_sha256 = adapter.get("artifact_sha256")
    if (
        adapter.get("family") != family
        or adapter.get("package") != "albert-memory"
        or adapter.get("package_version") != local_version
        or not isinstance(artifact_sha256, str)
        or not SHA256_PATTERN.fullmatch(artifact_sha256)
        or adapter.get("artifact_signature") != f"github-oidc-dsse:sha256:{artifact_sha256}"
    ):
        raise InstallError("Cloud adapter package provenance or host compatibility is untrusted")
    _https_url(adapter.get("artifact_url"), "Adapter artifact")

    parsed_link = urlsplit(link_url)
    base_path = parsed_link.path.rstrip("/")
    if not re.fullmatch(r".*/onboard/[A-Za-z0-9_-]{20,128}", base_path):
        raise InstallError("Cloud one-time link path is invalid")
    rest_path = base_path.rsplit("/onboard/", 1)[0]
    expected_rest = urlunsplit((parsed_link.scheme, parsed_link.netloc, rest_path, "", "")).rstrip(
        "/"
    )
    connection = unsigned.get("connection")
    redemption = unsigned.get("redemption")
    compensation = unsigned.get("compensation")
    reload_contract = unsigned.get("reload")
    revocation = unsigned.get("revocation")
    rollback = unsigned.get("rollback")
    verification = unsigned.get("verification")
    if (
        not all(isinstance(value, dict) for value in (connection, redemption, compensation))
        or set(connection) != {"authorization", "rest_url"}
        or set(redemption) != {"identity_bound", "method", "single_use", "url"}
        or set(compensation) != {"credential_specific", "method", "receipt_ttl_seconds", "url"}
    ):
        raise InstallError("Cloud adapter endpoints are malformed")
    try:
        expected_reload = {"command": list(load_host_target(family).install["reload"])}
    except (HostTargetError, KeyError, TypeError):
        raise InstallError("Cloud adapter host family is unsupported") from None
    rest_url = _https_url(connection.get("rest_url"), "Brain endpoint")
    redemption_url = _https_url(redemption.get("url"), "Redemption endpoint")
    compensation_url = _https_url(compensation.get("url"), "Compensation endpoint")
    expected_redeem = urlunsplit(
        (parsed_link.scheme, parsed_link.netloc, base_path + "/redeem/v2", parsed_link.query, "")
    )
    if (
        rest_url != expected_rest
        or connection.get("authorization") != "Bearer"
        or redemption.get("method") != "POST"
        or redemption.get("single_use") is not True
        or redemption.get("identity_bound") is not True
        or redemption_url != expected_redeem
        or compensation.get("method") != "POST"
        or compensation.get("credential_specific") is not True
        or compensation.get("receipt_ttl_seconds") != 600
        or compensation_url != f"{expected_rest}/onboard/compensate"
        or reload_contract != expected_reload
        or revocation
        != {
            "credential_id_issued_on_redemption": True,
            "independent_agent": True,
            "scope": "single-credential",
        }
        or rollback
        != {
            "compensate_before_commit": True,
            "ownership_marker": PLUGIN_ID,
            "restore_previous_state": True,
        }
        or verification
        != {
            "capture_recall_smoke": True,
            "health": "/v1/health/ready",
            "runtime_inspection": True,
        }
    ):
        raise InstallError("Cloud adapter endpoint binding is invalid")
    return CloudManifest(
        capabilities=capabilities,
        compensation_url=compensation_url,
        identity={key: str(identity[key]) for key in identity_keys},
        manifest_id=str(manifest_id),
        manifest_version=version,
        redemption_url=redemption_url,
        rest_url=rest_url,
    )


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _checked(runner: Runner, command: list[str], label: str) -> subprocess.CompletedProcess[str]:
    result = runner(command)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise InstallError(f"{label} failed{suffix}")
    return result


def _render_version(version: tuple[int, ...]) -> str:
    return ".".join(str(value) for value in version)


def _config_file_path(runner: Runner) -> Path:
    """`openclaw config file` prints health notices first; the path is the last line."""
    stdout = _checked(runner, ["openclaw", "config", "file"], "config discovery").stdout
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise InstallError("OpenClaw did not report its config file path")
    return Path(lines[-1]).expanduser()


def _detect_openclaw(
    runner: Runner, *, minimum: tuple[int, int, int] = MIN_OPENCLAW
) -> tuple[int, int, int]:
    result = _checked(runner, ["openclaw", "--version"], "OpenClaw detection")
    match = VERSION_PATTERN.search(result.stdout)
    if not match:
        raise InstallError("unsupported OpenClaw version output")
    version = tuple(int(value) for value in match.groups())
    if not minimum <= version < MAX_OPENCLAW:
        raise InstallError(
            f"OpenClaw {_render_version(version)} is unsupported; require "
            f">={_render_version(minimum)} and <{MAX_OPENCLAW[0]}"
        )
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
    event_classes: frozenset[str] = OPENCLAW_EVENTS,
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
                    local_event_classes=event_classes,
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
    cloud_metadata: dict[str, object] | None = None,
    package_version: str | None = None,
) -> None:
    version = _detect_openclaw(runner)
    installed_package_version = package_version or _package_version()
    try:
        target = negotiate_host_target(
            "openclaw", version, {"capture", "recall", "reload", "rollback", "uninstall"}
        )
        assert_owned_install_root(paths.root, expected_marker=target.ownership["marker"])
    except HostTargetError as exc:
        raise InstallError(str(exc)) from None
    token = _validate_secret(token)
    api_url = api_url.rstrip("/")
    if not api_url.startswith(("http://", "https://")):
        raise InstallError("Brain URL must use http:// or https://")

    previous_values: dict[str, str] | None = None
    if paths.root.exists():
        previous_values = _load_credentials(paths)
    queue_key = (
        previous_values.get("ALBERT_CAPTURE_QUEUE_KEY")
        if previous_values is not None
        else generate_queue_key()
    )
    if not queue_key:
        raise InstallError("existing capture queue key is invalid")
    check = brain_check or (
        lambda url, secret, key, workspace: _brain_check(url, secret, key, workspace, smoke=True)
    )
    check(api_url, token, queue_key, workspace_id)

    config_path = _config_file_path(runner)
    config_existed = config_path.exists()
    config_before = config_path.read_bytes() if config_existed else None
    config_mode = stat.S_IMODE(config_path.stat().st_mode) if config_existed else 0o600
    backup = paths.root.with_name(f".{paths.root.name}.rollback-{uuid.uuid4().hex}")
    staging = paths.root.with_name(f".{paths.root.name}.staging-{uuid.uuid4().hex}")
    plugin_path = Path(__file__).with_name("openclaw_plugin")
    previous_install = paths.root.exists()
    plugin_installed = False
    try:
        staged = InstallPaths(staging)
        _write_private(
            staged.credentials,
            _credentials_text(api_url, token, queue_key, paths.queue, workspace_id),
            0o600,
        )
        _write_private(staged.wrapper, _wrapper_text(paths.credentials), 0o700)
        if paths.queue.exists():
            shutil.copy2(paths.queue, staged.queue)
            os.chmod(staged.queue, 0o600)
        marker = {
            "owner": "albert-memory",
            "host": "openclaw",
            "host_target_schema": target.schema_version,
            "host_target_version": target.descriptor_version,
            "package_version": installed_package_version,
            "api_url": api_url,
            "openclaw_version": ".".join(str(value) for value in version),
        }
        if cloud_metadata is not None:
            marker["cloud"] = cloud_metadata
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
        plugin_installed = True
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
            ["openclaw", "plugins", "reload", PLUGIN_ID],
            "OpenClaw plugin reload",
        )
        _checked(
            runner,
            ["openclaw", "plugins", "inspect", PLUGIN_ID, "--runtime", "--json"],
            "OpenClaw runtime inspection",
        )
        check(api_url, token, queue_key, workspace_id)
    except Exception as exc:
        cleanup_error: Exception | None = None
        if plugin_installed and not previous_install:
            try:
                _checked(
                    runner,
                    ["openclaw", "plugins", "uninstall", PLUGIN_ID, "--force"],
                    "OpenClaw plugin rollback",
                )
            except Exception as rollback_exc:
                cleanup_error = rollback_exc
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
        if cleanup_error is not None:
            raise InstallError(
                f"{exc}; automatic plugin rollback failed ({cleanup_error}). "
                f"Run `openclaw plugins uninstall {PLUGIN_ID} --force`, then retry."
            ) from exc
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
    version = _detect_openclaw(runner)
    try:
        target = negotiate_host_target("openclaw", version, {"capture", "recall"})
        assert_owned_install_root(paths.root, expected_marker=target.ownership["marker"])
    except HostTargetError as exc:
        raise InstallError(str(exc)) from None
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
    version = _detect_openclaw(runner)
    try:
        target = negotiate_host_target("openclaw", version, {"uninstall"})
        assert_owned_install_root(paths.root, expected_marker=target.ownership["marker"])
    except HostTargetError as exc:
        raise InstallError(str(exc)) from None
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


CloudInstall = Callable[[str, str, str, dict[str, object]], None]


def read_private_link(link_stdin: bool, link_fd: int | None) -> str:
    """Read a one-time link from a hidden prompt, private stdin, or an open descriptor."""
    if link_stdin and link_fd is not None:
        raise InstallError("choose only one private link input mode")
    if link_fd is not None:
        with os.fdopen(os.dup(link_fd), encoding="utf-8") as stream:
            link = stream.read(8193)
    elif link_stdin:
        link = sys.stdin.read(8193)
    else:
        link = getpass.getpass("Albert Cloud one-time link: ")
    if len(link) > 8192:
        raise InstallError("Cloud one-time link is too long")
    return link.strip()


def redeem_cloud_link(
    *,
    link_url: str,
    family: str,
    agent_name: str,
    install: CloudInstall,
    trust: dict[str, object] | None = None,
    package_version: str | None = None,
    http: httpx.Client | None = None,
) -> None:
    """Redeem a signed Cloud one-time link and hand the credential to ``install``.

    ``install(rest_url, credential, workspace_id, cloud_metadata)`` must commit
    transactionally and prove the connection. If it raises, the freshly issued
    credential is revoked through Cloud's compensation receipt, so a failed
    connect never leaves a live credential behind. The caller detects and
    negotiates the host before calling, so an unsupported host never consumes
    the link.
    """
    from datetime import UTC, datetime, timedelta
    from urllib.parse import parse_qs, urlsplit, urlunsplit

    link_url = _https_url(_validate_secret(link_url.strip()), "Cloud one-time link")
    installed_package_version = package_version or _package_version()
    parsed = urlsplit(link_url)
    try:
        query = parse_qs(parsed.query, strict_parsing=True)
    except ValueError:
        raise InstallError("Cloud one-time link context is invalid") from None
    if set(query) != {"adapter_context"} or len(query["adapter_context"]) != 1:
        raise InstallError("Cloud one-time link context is invalid")
    base_path = parsed.path.rstrip("/")
    if not re.fullmatch(r".*/onboard/[A-Za-z0-9_-]{20,128}", base_path):
        raise InstallError("Cloud one-time link path is invalid")
    manifest_url = urlunsplit(
        (parsed.scheme, parsed.netloc, base_path + "/manifest/v2", parsed.query, "")
    )
    client = http or httpx.Client(timeout=15, follow_redirects=False)
    owns_client = http is None
    receipt: str | None = None
    compensation_url: str | None = None
    try:
        try:
            response = client.get(manifest_url, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise InstallError(f"Cloud manifest request failed ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise InstallError(f"Cloud manifest request was rejected ({response.status_code})")
        try:
            manifest_payload = response.json()
        except ValueError:
            raise InstallError("Cloud adapter manifest is malformed") from None
        manifest = _verify_manifest(
            manifest_payload,
            link_url=link_url,
            family=family,
            trust=trust,
            package_version=installed_package_version,
        )
        compensation_url = manifest.compensation_url
        redemption = {
            "agent_name": agent_name,
            "brain_id": manifest.identity["brain_id"],
            "capabilities": manifest.capabilities,
            "manifest_id": manifest.manifest_id,
            "manifest_version": manifest.manifest_version,
            "request_id": manifest.identity["request_id"],
            "workspace_id": manifest.identity["workspace_id"],
        }
        try:
            response = client.post(
                manifest.redemption_url,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                json=redemption,
            )
        except httpx.HTTPError as exc:
            raise InstallError(f"Cloud redemption failed ({type(exc).__name__})") from None
        if response.status_code != 201:
            raise InstallError(f"Cloud redemption was rejected ({response.status_code})")
        try:
            redeemed = response.json()
        except ValueError:
            raise InstallError("Cloud redemption response is malformed") from None
        if not isinstance(redeemed, dict):
            raise InstallError("Cloud redemption response is malformed")
        identity = redeemed.get("identity")
        compensation = redeemed.get("compensation")
        if isinstance(compensation, dict) and isinstance(compensation.get("receipt"), str):
            candidate_receipt = compensation["receipt"]
            if candidate_receipt and not any(char in candidate_receipt for char in "\r\n\0"):
                receipt = candidate_receipt
        credential = redeemed.get("credential")
        expected_identity = {
            "brain_id": manifest.identity["brain_id"],
            "request_id": manifest.identity["request_id"],
            "workspace_id": manifest.identity["workspace_id"],
        }
        if (
            set(redeemed)
            != {
                "capabilities",
                "compensation",
                "credential",
                "credential_notice",
                "identity",
                "manifest_version",
                "rest_url",
            }
            or not isinstance(identity, dict)
            or set(identity)
            != {
                "brain_id",
                "connection_id",
                "credential_id",
                "request_id",
                "workspace_id",
            }
            or any(identity.get(key) != value for key, value in expected_identity.items())
            or not all(
                isinstance(identity.get(key), str) and ID_PATTERN.fullmatch(identity[key])
                for key in ("connection_id", "credential_id")
            )
            or redeemed.get("manifest_version") != manifest.manifest_version
            or redeemed.get("rest_url") != manifest.rest_url
            or redeemed.get("capabilities") != manifest.capabilities
            or redeemed.get("credential_notice")
            != "Shown once; store only in the Albert-owned credential file."
            or not isinstance(credential, str)
            or not credential
            or any(character in credential for character in "\r\n\0")
            or not isinstance(compensation, dict)
            or set(compensation) != {"expires_at", "receipt"}
            or receipt is None
            or not isinstance(compensation.get("expires_at"), str)
        ):
            raise InstallError("Cloud redemption response identity does not match")
        try:
            compensation_expires = datetime.fromisoformat(
                compensation["expires_at"].replace("Z", "+00:00")
            )
        except ValueError:
            raise InstallError("Cloud compensation receipt lifetime is malformed") from None
        now = datetime.now(UTC)
        if (
            compensation_expires.utcoffset() != timedelta(0)
            or compensation_expires <= now
            or compensation_expires - now > timedelta(minutes=11)
        ):
            raise InstallError("Cloud compensation receipt lifetime is invalid")
        cloud_metadata = {
            "brain_id": identity["brain_id"],
            "connection_id": identity["connection_id"],
            "credential_id": identity["credential_id"],
            "manifest_id": manifest.manifest_id,
            "manifest_version": manifest.manifest_version,
            "request_id": identity["request_id"],
            "rest_url": manifest.rest_url,
            "workspace_id": identity["workspace_id"],
        }
        install(manifest.rest_url, credential, manifest.identity["workspace_id"], cloud_metadata)
    except Exception as connect_exc:
        if receipt is not None and compensation_url is not None:
            try:
                compensated = client.post(
                    compensation_url,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    json={"receipt": receipt},
                )
                if compensated.status_code != 200:
                    raise InstallError(
                        f"credential compensation was rejected ({compensated.status_code})"
                    )
                try:
                    compensation_result = compensated.json()
                except ValueError:
                    raise InstallError("credential compensation response is malformed") from None
                expected_compensation = {
                    "connection_id": identity.get("connection_id")
                    if isinstance(identity, dict)
                    else None,
                    "credential_id": identity.get("credential_id")
                    if isinstance(identity, dict)
                    else None,
                    "revoked": True,
                }
                if compensation_result != expected_compensation:
                    raise InstallError("credential compensation response did not match")
            except (httpx.HTTPError, InstallError) as compensation_exc:
                raise InstallError(
                    f"{connect_exc}; replacement credential compensation failed "
                    f"({type(compensation_exc).__name__}); revoke the failed connection in Cloud"
                ) from connect_exc
        if isinstance(connect_exc, InstallError):
            raise
        raise InstallError(f"connection failed safely ({type(connect_exc).__name__})") from None
    finally:
        if owns_client:
            client.close()


def connect_openclaw(
    *,
    link_url: str,
    paths: InstallPaths,
    runner: Runner = _run,
    trust: dict[str, object] | None = None,
    package_version: str | None = None,
    http: httpx.Client | None = None,
) -> None:
    version = _detect_openclaw(runner)
    try:
        negotiate_host_target(
            "openclaw",
            version,
            {"capture", "recall", "reload", "rollback", "rotate", "revoke"},
        )
    except HostTargetError as exc:
        raise InstallError(str(exc)) from None
    installed_package_version = package_version or _package_version()
    redeem_cloud_link(
        link_url=link_url,
        family="openclaw",
        agent_name="Native OpenClaw",
        install=lambda rest_url, credential, workspace_id, cloud: install_openclaw(
            api_url=rest_url,
            token=credential,
            workspace_id=workspace_id,
            paths=paths,
            runner=runner,
            cloud_metadata=cloud,
            package_version=installed_package_version,
        ),
        trust=trust,
        package_version=installed_package_version,
        http=http,
    )


# --- Classifier agent (ALBERT_LLM_PROVIDER=openclaw) ----------------------------
#
# A Brain can classify memories through an OpenClaw gateway agent instead of its
# own model credential: Albert posts to `/v1/responses` as `openclaw/<agent>` and
# OpenClaw supplies the model and sign-in. The agent is deliberately tiny: no
# tools, no plugins, a workspace that only says "reply with one JSON object".
# Everything written here carries Albert's ownership marker so that only
# Albert-made agents and workspaces are ever changed or removed.

CLASSIFIER_AGENT_ID = "albert-classifier"
# The agent needs only `openclaw agents add --non-interactive`, `openclaw config
# set` and the gateway's `/v1/responses` endpoint, all present well before the
# capture plugin's host-target range; verified on 2026.6.35.
CLASSIFIER_MIN_OPENCLAW = (2026, 6, 35)
CLASSIFIER_AGENT_NAME = "Albert classifier"
CLASSIFIER_WORKSPACE = Path("~/.openclaw/workspace-albert-classifier")
CLASSIFIER_OWNED_MARKER = ".albert-owned.json"
# The embedded runner refuses an agent whose allowlist leaves no callable tool,
# and this agent gets none from its requests, so it keeps the read-only
# `session_status`: no plugin tools (no memory access), no messaging.
CLASSIFIER_TOOLS = {"profile": "minimal", "deny": ["group:plugins", "message"]}
AGENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
Probe = Callable[[str, str, str], None]

CLASSIFIER_AGENTS_MD = """# AGENTS.md

## What this agent is

`{agent_id}` serves Albert's memory classifier. The Albert worker
(`ALBERT_LLM_PROVIDER=openclaw`) posts one memory at a time to the gateway
Responses endpoint (`model: openclaw/{agent_id}`) with the classification
instruction in `instructions` and the memory text as `input`.

## Rules

- Reply with exactly one JSON object and nothing else: no prose, no code
  fences, no explanation. The per-request `instructions` define the keys.
- Never try to read files, run commands, browse, message anyone or reach other
  agents. You have no such tools and must not ask for them.
- Keep nothing in this workspace. There is no memory, status or notes file.

This workspace is installed and owned by Albert (`albert-capture
classifier-agent`); edits are overwritten on reinstall.
"""

CLASSIFIER_SOUL_MD = """# SOUL.md

You are a JSON-only classifier for Albert, the shared memory service.

Each request's `instructions` tell you what to label and which values are
allowed. The `input` is the text of one memory. Return exactly one JSON object
and nothing else. Never include credentials or secrets in the output.
"""

CLASSIFIER_HEARTBEAT_MD = (
    "<!-- Heartbeat template; comments-only content prevents scheduled heartbeat API calls. -->\n"
)


def _read_openclaw_config(runner: Runner) -> tuple[Path, dict[str, object]]:
    config_path = _config_file_path(runner)
    if not config_path.exists():
        raise InstallError(f"OpenClaw config {config_path} does not exist")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallError(f"OpenClaw config is unreadable ({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise InstallError("OpenClaw config is not a JSON object")
    return config_path, data


def _agent_index(data: dict[str, object], agent_id: str) -> int | None:
    agents = data.get("agents")
    entries = agents.get("list") if isinstance(agents, dict) else None
    if not isinstance(entries, list):
        return None
    for index, entry in enumerate(entries):
        if isinstance(entry, dict) and entry.get("id") == agent_id:
            return index
    return None


def _gateway_target(data: dict[str, object]) -> tuple[str, str]:
    gateway = data.get("gateway")
    if not isinstance(gateway, dict):
        raise InstallError("OpenClaw config has no gateway section")
    port = gateway.get("port", 18789)
    auth = gateway.get("auth")
    token = auth.get("token") if isinstance(auth, dict) else None
    if not isinstance(token, str) or not token:
        raise InstallError(
            "gateway.auth.token is not set in openclaw.json; the Brain authenticates "
            "to the gateway with it"
        )
    return f"http://127.0.0.1:{port}", token


def _workspace_owned(workspace: Path) -> bool:
    marker = workspace / CLASSIFIER_OWNED_MARKER
    try:
        return json.loads(marker.read_text(encoding="utf-8")).get("owner") == PLUGIN_ID
    except (OSError, ValueError, AttributeError):
        return False


def _assert_workspace_ownable(workspace: Path) -> None:
    if workspace.exists() and any(workspace.iterdir()) and not _workspace_owned(workspace):
        raise InstallError(f"workspace {workspace} exists and is not owned by Albert")


def _write_classifier_workspace(workspace: Path, agent_id: str) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text(
        CLASSIFIER_AGENTS_MD.format(agent_id=agent_id), encoding="utf-8"
    )
    _write_private(workspace / "SOUL.md", CLASSIFIER_SOUL_MD, 0o600)
    _write_private(workspace / "HEARTBEAT.md", CLASSIFIER_HEARTBEAT_MD, 0o600)
    _write_private(
        workspace / CLASSIFIER_OWNED_MARKER,
        json.dumps({"owner": PLUGIN_ID, "agent_id": agent_id}, sort_keys=True) + "\n",
        0o600,
    )


def probe_classifier_agent(gateway_url: str, token: str, agent_id: str) -> None:
    """One live JSON-only call through the gateway; raises InstallError on any problem."""
    from albert.openclaw_llm import OpenClawClient, OpenClawRequestError

    client = OpenClawClient(gateway_url, token, agent_id)
    try:
        reply = client.complete_json(
            "Reply with exactly one JSON object and nothing else.",
            'Return {"ok": true}.',
        )
    except OpenClawRequestError as exc:
        raise InstallError(f"classifier agent probe failed: {exc}") from None
    finally:
        client.close()
    if reply.get("ok") is not True:
        raise InstallError(f"classifier agent probe returned an unexpected object: {reply!r}")


def install_classifier_agent(
    *,
    agent_id: str = CLASSIFIER_AGENT_ID,
    workspace: Path = CLASSIFIER_WORKSPACE,
    model: str | None = None,
    runner: Runner = _run,
    probe: Probe | None = probe_classifier_agent,
) -> str:
    """Create (or repair) the tool-less classifier agent; return the gateway URL.

    Rolls the config file, the agent and the workspace back if any step fails.
    """
    if not AGENT_ID_PATTERN.match(agent_id):
        raise InstallError("agent id must be lowercase letters, digits, '-' or '_'")
    _detect_openclaw(runner, minimum=CLASSIFIER_MIN_OPENCLAW)
    workspace = workspace.expanduser()
    _assert_workspace_ownable(workspace)
    config_path, data = _read_openclaw_config(runner)
    config_before = config_path.read_bytes()
    config_mode = stat.S_IMODE(config_path.stat().st_mode)
    gateway_url, token = _gateway_target(data)
    if model is None:
        defaults = data.get("agents", {}).get("defaults", {}) if isinstance(data, dict) else {}
        primary = defaults.get("model", {}).get("primary") if isinstance(defaults, dict) else None
        if not isinstance(primary, str) or not primary:
            raise InstallError(
                "agents.defaults.model.primary is not set in openclaw.json; pass --model"
            )
        model = primary

    created_agent = False
    created_workspace = not workspace.exists()
    try:
        _write_classifier_workspace(workspace, agent_id)
        if _agent_index(data, agent_id) is None:
            _checked(
                runner,
                [
                    "openclaw",
                    "agents",
                    "add",
                    agent_id,
                    "--workspace",
                    str(workspace),
                    "--model",
                    model,
                    "--non-interactive",
                    "--json",
                ],
                "OpenClaw agent creation",
            )
            created_agent = True
            _, data = _read_openclaw_config(runner)
        index = _agent_index(data, agent_id)
        if index is None:
            raise InstallError(f"OpenClaw did not record agent {agent_id} in agents.list")
        settings = {
            f"agents.list.{index}.workspace": str(workspace),
            f"agents.list.{index}.identity": {"name": CLASSIFIER_AGENT_NAME},
            f"agents.list.{index}.tools": CLASSIFIER_TOOLS,
            # The default Codex harness for openai/* models ignores per-request
            # instructions on the Responses endpoint; the OpenClaw runtime honours them.
            f"agents.list.{index}.models": {model: {"agentRuntime": {"id": "openclaw"}}},
            "gateway.http.endpoints.responses.enabled": True,
        }
        for path, value in settings.items():
            _checked(
                runner,
                [
                    "openclaw",
                    "config",
                    "set",
                    path,
                    json.dumps(value, separators=(",", ":")),
                    "--strict-json",
                ],
                f"OpenClaw config {path}",
            )
        _checked(runner, ["openclaw", "config", "validate"], "OpenClaw config validation")
        if probe is not None:
            probe(gateway_url, token, agent_id)
    except Exception:
        if created_agent:
            runner(["openclaw", "agents", "delete", agent_id, "--force", "--json"])
            _remove_empty_agent_state(config_path, agent_id)
        temporary = config_path.with_name(f".{config_path.name}.albert-rollback")
        temporary.write_bytes(config_before)
        os.chmod(temporary, config_mode)
        os.replace(temporary, config_path)
        if created_workspace and workspace.exists():
            shutil.rmtree(workspace)
        raise
    return gateway_url


def remove_classifier_agent(
    *,
    agent_id: str = CLASSIFIER_AGENT_ID,
    workspace: Path = CLASSIFIER_WORKSPACE,
    runner: Runner = _run,
) -> None:
    """Delete the classifier agent and its workspace, only if Albert installed them."""
    _detect_openclaw(runner, minimum=CLASSIFIER_MIN_OPENCLAW)
    workspace = workspace.expanduser()
    if not _workspace_owned(workspace):
        raise InstallError(f"workspace {workspace} is not owned by Albert; nothing removed")
    config_path, data = _read_openclaw_config(runner)
    if _agent_index(data, agent_id) is not None:
        _checked(
            runner,
            ["openclaw", "agents", "delete", agent_id, "--force", "--json"],
            "OpenClaw agent removal",
        )
    if workspace.exists():
        shutil.rmtree(workspace)
    _remove_empty_agent_state(config_path, agent_id)


def _remove_empty_agent_state(config_path: Path, agent_id: str) -> None:
    """`openclaw agents delete` leaves an empty state directory behind; drop it."""
    state = config_path.parent / "agents" / agent_id
    if state.is_dir() and not any(state.iterdir()):
        state.rmdir()


def _default_paths() -> InstallPaths:
    return InstallPaths(Path("~/.openclaw/albert").expanduser())


@app.command("connect")
def connect_command(
    link_stdin: bool = typer.Option(
        False,
        "--link-stdin",
        help="Read the one-time link from private standard input.",
    ),
    link_fd: int | None = typer.Option(
        None,
        "--link-fd",
        min=0,
        help="Read the one-time link from an already-open private file descriptor.",
    ),
) -> None:
    """Privately redeem a Cloud one-time link and connect OpenClaw."""
    try:
        link = read_private_link(link_stdin, link_fd)
        connect_openclaw(link_url=link, paths=_default_paths())
        typer.echo("Albert Memory connected; reload, live capture, and fresh recall passed.")
    except (InstallError, OSError) as exc:
        typer.echo(f"Connect failed safely: {exc}", err=True)
        raise typer.Exit(1) from None


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


@app.command("classifier-agent")
def classifier_agent_command(
    agent_id: str = typer.Option(CLASSIFIER_AGENT_ID, "--agent-id"),
    workspace: Path = typer.Option(CLASSIFIER_WORKSPACE, "--workspace"),
    model: str | None = typer.Option(
        None, "--model", help="Model id for the agent; defaults to agents.defaults.model.primary."
    ),
    remove: bool = typer.Option(False, "--remove", help="Delete the Albert-owned agent instead."),
    skip_probe: bool = typer.Option(
        False, "--skip-probe", help="Do not make one live call through the gateway."
    ),
) -> None:
    """Install the tool-less agent a Brain uses with ALBERT_LLM_PROVIDER=openclaw."""
    try:
        if remove:
            remove_classifier_agent(agent_id=agent_id, workspace=workspace)
            typer.echo(f"Removed OpenClaw agent {agent_id} and its Albert-owned workspace.")
            return
        gateway_url = install_classifier_agent(
            agent_id=agent_id,
            workspace=workspace,
            model=model,
            probe=None if skip_probe else probe_classifier_agent,
        )
        typer.echo(
            f"OpenClaw agent {agent_id} is ready"
            + ("." if skip_probe else "; a live JSON classification passed.")
        )
        typer.echo(
            "On the Brain set ALBERT_LLM_PROVIDER=openclaw, "
            f"ALBERT_OPENCLAW_AGENT_ID={agent_id}, ALBERT_OPENCLAW_GATEWAY_URL to this host's "
            f"gateway ({gateway_url} here; http://host.docker.internal:<port> from Compose) and "
            "ALBERT_OPENCLAW_GATEWAY_TOKEN to gateway.auth.token from openclaw.json, then "
            "restart api and worker."
        )
    except (InstallError, OSError) as exc:
        typer.echo(f"Classifier agent setup failed safely: {exc}", err=True)
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
