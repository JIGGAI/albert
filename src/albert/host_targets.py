"""Versioned, fail-closed host lifecycle target contracts."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class HostTargetError(RuntimeError):
    """A host target cannot be trusted or negotiated."""


HOST_TARGET_SCHEMA = "com.jiggai.albert.host-target"
HOST_TARGET_SCHEMA_VERSION = 1
CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,99}$")
VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
RANGE_PATTERN = re.compile(r"^(>=|>|<=|<|==)(\d+)\.(\d+)\.(\d+)$")
TARGET_PATH = Path(__file__).with_name("host_targets")


@dataclass(frozen=True)
class HostTarget:
    target_id: str
    schema_version: int
    descriptor_version: str
    package: dict[str, str]
    host: dict[str, object]
    hooks: dict[str, object]
    install: dict[str, object]
    credentials: dict[str, object]
    ownership: dict[str, str]
    lifecycle: dict[str, object]
    probes: tuple[str, ...]
    capabilities: tuple[str, ...]


def _version(value: object, label: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not (match := VERSION_PATTERN.fullmatch(value)):
        raise HostTargetError(f"{label} must be an exact semantic version")
    return tuple(int(part) for part in match.groups())


def _version_in_range(version: tuple[int, int, int], declared: object) -> bool:
    if not isinstance(declared, str):
        return False
    clauses = [part.strip() for part in declared.split(",") if part.strip()]
    if not clauses:
        return False
    for clause in clauses:
        match = RANGE_PATTERN.fullmatch(clause)
        if not match:
            return False
        target = tuple(int(value) for value in match.groups()[1:])
        if not {
            ">=": version >= target,
            ">": version > target,
            "<=": version <= target,
            "<": version < target,
            "==": version == target,
        }[match.group(1)]:
            return False
    return True


def _string_map(value: object, keys: set[str], label: str) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or not all(isinstance(item, str) and item for item in value.values())
    ):
        raise HostTargetError(f"host target {label} is malformed")
    return dict(value)


def validate_host_target(payload: object) -> HostTarget:
    """Validate every field before a target can cause host or filesystem changes."""
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "schema_version",
        "descriptor_version",
        "target_id",
        "enabled",
        "package",
        "host",
        "hooks",
        "install",
        "credentials",
        "ownership",
        "lifecycle",
        "probes",
        "capabilities",
    }:
        raise HostTargetError("host target contains unknown or missing fields")
    if (
        payload["schema"] != HOST_TARGET_SCHEMA
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
    ):
        raise HostTargetError("host target schema or version is unsupported")
    if payload["enabled"] is not True:
        raise HostTargetError("host target is disabled")
    target_id = payload["target_id"]
    if not isinstance(target_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,49}", target_id):
        raise HostTargetError("host target identifier is invalid")
    _version(payload["descriptor_version"], "descriptor_version")

    package = _string_map(
        payload["package"], {"distribution", "version", "artifact_provenance"}, "package"
    )
    _version(package["version"], "package.version")
    if package["artifact_provenance"] != "github-oidc-dsse+sha256":
        raise HostTargetError("host target package provenance is unsupported")

    host = payload["host"]
    if not isinstance(host, dict) or set(host) != {"family", "tested", "supported"}:
        raise HostTargetError("host target host compatibility is malformed")
    tested = _version(host["tested"], "host.tested")
    if host["family"] != target_id or not _version_in_range(tested, host["supported"]):
        raise HostTargetError("host target tested release is outside its supported range")

    hooks = payload["hooks"]
    if (
        not isinstance(hooks, dict)
        or set(hooks) != {"fidelity", "present", "absent"}
        or hooks["fidelity"] not in {"full", "reduced"}
        or not all(isinstance(hooks[key], list) for key in ("present", "absent"))
    ):
        raise HostTargetError("host target hook fidelity is malformed")
    present = hooks["present"]
    absent = hooks["absent"]
    if (
        not all(
            isinstance(item, str) and CAPABILITY_PATTERN.fullmatch(item)
            for item in present + absent
        )
        or len(present) != len(set(present))
        or len(absent) != len(set(absent))
        or set(present) & set(absent)
        or (hooks["fidelity"] == "full" and absent)
    ):
        raise HostTargetError("host target hooks are contradictory or fabricated")

    install = payload["install"]
    if not isinstance(install, dict) or set(install) != {"root", "reload", "uninstall"}:
        raise HostTargetError("host target install contract is malformed")
    root = install["root"]
    if (
        not isinstance(root, str)
        or not root.startswith("~/")
        or ".." in PurePosixPath(root[2:]).parts
        or not all(
            isinstance(install[key], list)
            and install[key]
            and all(isinstance(arg, str) and arg for arg in install[key])
            for key in ("reload", "uninstall")
        )
    ):
        raise HostTargetError("host target install path or commands are unsafe")

    credentials = payload["credentials"]
    if credentials != {
        "acquisition": ["hidden_prompt", "private_stdin", "private_fd"],
        "storage": "owned_file_0600",
        "forbidden_surfaces": [
            "args",
            "ordinary_env",
            "host_config",
            "logs",
            "diagnostics",
            "artifacts",
        ],
    }:
        raise HostTargetError("host target credential handling is unsafe")
    ownership = _string_map(payload["ownership"], {"marker", "scope"}, "ownership")
    if ownership["scope"] != "target-owned-tree-and-entry":
        raise HostTargetError("host target ownership scope is unsafe")

    lifecycle = payload["lifecycle"]
    if (
        not isinstance(lifecycle, dict)
        or set(lifecycle) != {"upgrade", "rotate", "revoke", "rollback", "direct_brain"}
        or not all(value is True for value in lifecycle.values())
    ):
        raise HostTargetError("host target lifecycle contract is incomplete")
    probes = payload["probes"]
    capabilities = payload["capabilities"]
    if (
        not isinstance(probes, list)
        or probes != ["host_config", "runtime", "brain_ready", "capture_recall"]
        or not isinstance(capabilities, list)
        or capabilities != sorted(set(capabilities))
        or not capabilities
        or not all(
            isinstance(item, str) and CAPABILITY_PATTERN.fullmatch(item) for item in capabilities
        )
    ):
        raise HostTargetError("host target probes or capabilities are invalid")
    return HostTarget(
        target_id=target_id,
        schema_version=payload["schema_version"],
        descriptor_version=payload["descriptor_version"],
        package=package,
        host=dict(host),
        hooks=dict(hooks),
        install=dict(install),
        credentials=dict(credentials),
        ownership=ownership,
        lifecycle=dict(lifecycle),
        probes=tuple(probes),
        capabilities=tuple(capabilities),
    )


def load_host_target(target_id: str) -> HostTarget:
    """Load only explicitly shipped targets; unknown targets fail closed."""
    if target_id not in {"openclaw", "cursor"}:
        raise HostTargetError(f"unknown or disabled host target: {target_id}")
    try:
        payload = json.loads((TARGET_PATH / f"{target_id}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HostTargetError("host target descriptor is unavailable or invalid") from exc
    target = validate_host_target(payload)
    if target.target_id != target_id:
        raise HostTargetError("host target descriptor identity mismatch")
    return target


def negotiate_host_target(
    target_id: str,
    host_version: tuple[int, int, int],
    required_capabilities: set[str],
    *,
    target: HostTarget | None = None,
) -> HostTarget:
    negotiated = target or load_host_target(target_id)
    if negotiated.target_id != target_id or not _version_in_range(
        host_version, negotiated.host["supported"]
    ):
        raise HostTargetError("host target version is unsupported")
    if not required_capabilities <= set(negotiated.capabilities):
        raise HostTargetError("host target lacks required capabilities")
    return negotiated


def assert_owned_install_root(root: Path, *, expected_marker: str) -> None:
    """Reject foreign ownership, symlink traversal, and widened modes."""
    if root.is_symlink():
        raise HostTargetError("host target install root must not be a symlink")
    current = root
    while not current.exists() and current != current.parent:
        current = current.parent
    while current != current.parent:
        if current.is_symlink():
            raise HostTargetError("host target install path contains a symlink")
        current = current.parent
    if not root.exists():
        return
    marker_path = root / "install.json"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HostTargetError("host target ownership marker is unavailable") from exc
    if not isinstance(marker, dict) or marker.get("owner") != expected_marker:
        raise HostTargetError("host target installation has foreign ownership")
    for private_path in (marker_path, root / "credentials.env", root / "credentials.json"):
        if private_path.exists() and stat.S_IMODE(os.stat(private_path).st_mode) != 0o600:
            raise HostTargetError("host target private file mode is unsafe")
