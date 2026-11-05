#!/usr/bin/env python3
"""Build-time contract checks and provenance for the OpenClaw package."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import tomllib
import zipfile
from pathlib import Path

DESCRIPTOR = "albert/openclaw_package.json"
REQUIRED = {
    "albert/capture_installer.py",
    "albert/host_targets.py",
    "albert/host_targets/openclaw.json",
    "albert/openclaw_adapter.py",
    "albert/openclaw_adapter_trust.json",
    "albert/openclaw_package.json",
    "albert/openclaw_plugin/index.js",
    "albert/openclaw_plugin/openclaw.plugin.json",
    "albert/openclaw_plugin/package.json",
}
SHA = re.compile(r"[0-9a-f]{40}")
KEY_ID = re.compile(r"[A-Za-z0-9._:-]{1,200}")
PUBLIC_KEY = re.compile(r"[A-Za-z0-9_-]{43}")


def fail(message: str) -> None:
    raise SystemExit(f"OpenClaw package contract failed: {message}")


def trust(raw: bytes) -> set[str]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"invalid trust JSON: {exc}")
    if not isinstance(value, dict) or set(value) != {"manifest_keys"}:
        fail("trust metadata fields drifted")
    keys = value["manifest_keys"]
    if not isinstance(keys, dict) or not keys or len(keys) > 8:
        fail("manifest trust set must contain one to eight keys")
    for key_id, encoded in keys.items():
        if not isinstance(key_id, str) or not KEY_ID.fullmatch(key_id):
            fail("manifest trust key ID is invalid")
        if not isinstance(encoded, str) or not PUBLIC_KEY.fullmatch(encoded):
            fail("manifest public key is invalid")
        try:
            decoded = base64.b64decode(encoded + "=", altchars=b"-_", validate=True)
        except (binascii.Error, ValueError):
            fail("manifest public key is invalid")
        if len(decoded) != 32:
            fail("manifest public key is invalid")
    return set(keys)


def descriptor(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"invalid descriptor JSON: {exc}")
    if not isinstance(value, dict):
        fail("descriptor must be an object")
    expected = {
        "schema", "package", "package_version", "family", "owner",
        "manifest_contract", "supported_openclaw", "install_root",
        "manifest_key_ids",
        "rollback_source_template", "components",
    }
    if set(value) != expected:
        fail("descriptor fields drifted")
    if value["schema"] != 1 or value["package"] != "albert-memory":
        fail("descriptor identity is invalid")
    if value["owner"] != "JIGGAI/albert" or value["family"] != "openclaw":
        fail("ownership metadata is invalid")
    if value["manifest_contract"] != ">=2,<3":
        fail("manifest v2 compatibility is missing")
    if value["supported_openclaw"] != ">=2026.9.6,<2027.0.0":
        fail("OpenClaw compatibility declaration drifted")
    key_ids = value.get("manifest_key_ids")
    if (
        not isinstance(key_ids, list)
        or not key_ids
        or len(key_ids) != len(set(key_ids))
        or any(not isinstance(item, str) or not KEY_ID.fullmatch(item) for item in key_ids)
    ):
        fail("manifest trust key inventory is invalid")
    components = value.get("components")
    if not isinstance(components, dict) or set(components) != {
        "connector",
        "adapter",
        "host_target",
        "plugin_manifest",
        "plugin_runtime",
        "ownership_marker",
    }:
        fail("component inventory is incomplete")
    if components["ownership_marker"] != "albert-memory":
        fail("ownership marker drifted")
    if components["host_target"] != "albert/host_targets/openclaw.json":
        fail("host target descriptor drifted")
    return value


def source(root: Path) -> None:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    meta = descriptor((root / "src" / DESCRIPTOR).read_bytes())
    if project["project"]["version"] != meta["package_version"]:
        fail("project and package descriptor versions differ")
    scripts = project["project"]["scripts"]
    if scripts.get("albert-capture") != meta["components"]["connector"]:
        fail("connector entry point drifted")
    if scripts.get("albert-openclaw-adapter") != meta["components"]["adapter"]:
        fail("adapter entry point drifted")
    trust_keys = trust((root / "src/albert/openclaw_adapter_trust.json").read_bytes())
    if trust_keys != set(meta["manifest_key_ids"]):
        fail("manifest trust keys differ from the package descriptor")
    plugin = json.loads((root / "src/albert/openclaw_plugin/package.json").read_text())
    if plugin.get("version") != meta["package_version"]:
        fail("plugin and package versions differ")
    if plugin.get("openclaw", {}).get("compat", {}).get("pluginApi") != ">=2026.9.6 <2027":
        fail("native plugin compatibility drifted")
    target = json.loads((root / "src" / meta["components"]["host_target"]).read_text())
    if (
        target.get("schema") != "com.jiggai.albert.host-target"
        or target.get("schema_version") != 1
        or target.get("target_id") != "openclaw"
        or target.get("package", {}).get("version") != meta["package_version"]
        or target.get("host", {}).get("supported") != meta["supported_openclaw"]
    ):
        fail("host target identity or compatibility drifted")


def wheel(path: Path) -> tuple[dict[str, object], str]:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        missing = REQUIRED - names
        if missing:
            fail(f"wheel is missing: {', '.join(sorted(missing))}")
        meta = descriptor(archive.read(DESCRIPTOR))
        trust_keys = trust(archive.read("albert/openclaw_adapter_trust.json"))
        if trust_keys != set(meta["manifest_key_ids"]):
            fail("wheel trust keys differ from the package descriptor")
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
        "family": "openclaw",
        "package": meta["package"],
        "package_version": meta["package_version"],
        "supported_openclaw": meta["supported_openclaw"],
        "manifest_contract": meta["manifest_contract"],
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
    expected = {
        "schema": 1,
        "artifact": path.name,
        "artifact_sha256": digest,
        "artifact_signature": f"github-oidc-dsse:sha256:{digest}",
        "family": "openclaw",
        "package": meta["package"],
        "package_version": meta["package_version"],
        "supported_openclaw": meta["supported_openclaw"],
        "manifest_contract": meta["manifest_contract"],
        "source_revision": revision,
        "rollback_source": str(meta["rollback_source_template"]).format(
            source_revision=revision
        ) if isinstance(revision, str) else "",
    }
    if not isinstance(revision, str) or not SHA.fullmatch(revision) or value != expected:
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
