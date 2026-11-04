#!/usr/bin/env python3
"""Build-time contract checks and immutable provenance for Claude Code."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
import zipfile
from pathlib import Path

DESCRIPTOR = "albert/claude_package.json"
REQUIRED = {
    "albert/claude_adapter.py",
    "albert/claude_lifecycle.py",
    "albert/claude_package.json",
    "albert/claude_plugin/.claude-plugin/plugin.json",
    "albert/claude_plugin/.mcp.json",
    "albert/claude_plugin/hooks/hooks.json",
    "albert/host_targets.py",
    "albert/host_targets/claude-code.json",
}
SHA = re.compile(r"[0-9a-f]{40}")


def fail(message: str) -> None:
    raise SystemExit(f"Claude package contract failed: {message}")


def descriptor(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"invalid descriptor JSON: {exc}")
    expected = {
        "schema",
        "package",
        "package_version",
        "family",
        "owner",
        "supported_claude",
        "tested_claude",
        "hook_fidelity",
        "install_root",
        "rollback_source_template",
        "components",
    }
    if not isinstance(value, dict) or set(value) != expected:
        fail("descriptor fields drifted")
    if (
        value["schema"] != 1
        or value["package"] != "albert-memory"
        or value["family"] != "claude-code"
        or value["owner"] != "JIGGAI/albert"
        or value["supported_claude"] != ">=2.1.78,<2.2.0"
        or value["tested_claude"] != "2.1.312"
        or value["hook_fidelity"] != "full-with-best-effort-failure-hook"
    ):
        fail("identity, compatibility, or fidelity metadata drifted")
    components = value.get("components")
    if not isinstance(components, dict) or set(components) != {
        "adapter",
        "connector",
        "host_target",
        "ownership_marker",
        "plugin_manifest",
    }:
        fail("component inventory is incomplete")
    return value


def source(root: Path) -> None:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    meta = descriptor((root / "src" / DESCRIPTOR).read_bytes())
    if project["project"]["version"] != meta["package_version"]:
        fail("project and package descriptor versions differ")
    scripts = project["project"]["scripts"]
    components = meta["components"]
    if (
        scripts.get("albert-claude-adapter") != components["adapter"]
        or scripts.get("albert-claude-lifecycle") != components["connector"]
    ):
        fail("Claude entry points drifted")
    plugin = json.loads(
        (root / "src/albert/claude_plugin/.claude-plugin/plugin.json").read_text()
    )
    target = json.loads((root / "src" / components["host_target"]).read_text())
    if plugin.get("version") != meta["package_version"]:
        fail("plugin and package versions differ")
    if (
        target.get("target_id") != "claude-code"
        or target.get("package", {}).get("version") != meta["package_version"]
        or target.get("host", {}).get("tested") != meta["tested_claude"]
        or target.get("host", {}).get("supported") != meta["supported_claude"]
        or target.get("hooks", {}).get("fidelity") != "reduced"
        or target.get("hooks", {}).get("absent") != ["reliable_failure_delivery"]
    ):
        fail("host target compatibility or truthful fidelity drifted")


def wheel(path: Path) -> tuple[dict[str, object], str]:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        missing = REQUIRED - names
        if missing:
            fail(f"wheel is missing: {', '.join(sorted(missing))}")
        meta = descriptor(archive.read(DESCRIPTOR))
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            fail("wheel must contain one METADATA file")
        metadata = archive.read(metadata_names[0]).decode("utf-8")
        if f"Version: {meta['package_version']}\n" not in metadata:
            fail("wheel metadata version differs from descriptor")
    return meta, hashlib.sha256(path.read_bytes()).hexdigest()


def emit(path: Path, revision: str, output: Path) -> None:
    if not SHA.fullmatch(revision):
        fail("source revision must be a full lowercase commit SHA")
    meta, digest = wheel(path)
    payload = {
        "schema": 1,
        "artifact": path.name,
        "artifact_sha256": digest,
        "artifact_signature": f"github-oidc-dsse:sha256:{digest}",
        "family": "claude-code",
        "package": meta["package"],
        "package_version": meta["package_version"],
        "supported_claude": meta["supported_claude"],
        "tested_claude": meta["tested_claude"],
        "source_revision": revision,
        "rollback_source": str(meta["rollback_source_template"]).format(
            source_revision=revision
        ),
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def verify(path: Path, provenance: Path) -> None:
    meta, digest = wheel(path)
    value = json.loads(provenance.read_text(encoding="utf-8"))
    revision = value.get("source_revision")
    if not isinstance(revision, str) or not SHA.fullmatch(revision):
        fail("source revision is not immutable")
    expected = {
        "schema": 1,
        "artifact": path.name,
        "artifact_sha256": digest,
        "artifact_signature": f"github-oidc-dsse:sha256:{digest}",
        "family": "claude-code",
        "package": meta["package"],
        "package_version": meta["package_version"],
        "supported_claude": meta["supported_claude"],
        "tested_claude": meta["tested_claude"],
        "source_revision": revision,
        "rollback_source": str(meta["rollback_source_template"]).format(
            source_revision=revision
        ),
    }
    if value != expected:
        fail("provenance is incomplete, mutable, or does not match the wheel")


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    source_parser = commands.add_parser("source")
    source_parser.add_argument("root", type=Path, nargs="?", default=Path("."))
    emit_parser = commands.add_parser("emit")
    emit_parser.add_argument("wheel", type=Path)
    emit_parser.add_argument("--source-revision", required=True)
    emit_parser.add_argument("--output", required=True, type=Path)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("wheel", type=Path)
    verify_parser.add_argument("provenance", type=Path)
    args = parser.parse_args()
    if args.command == "source":
        source(args.root)
    elif args.command == "emit":
        emit(args.wheel, args.source_revision, args.output)
    else:
        verify(args.wheel, args.provenance)


if __name__ == "__main__":
    main()
