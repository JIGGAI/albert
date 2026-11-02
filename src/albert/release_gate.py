"""Machine-checkable automatic-memory release evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from importlib import metadata, resources
from pathlib import Path
from typing import Any

HOSTS = frozenset({"openclaw", "codex", "claude-code", "cursor"})
GOLDEN_FLOW = frozenset(
    {"start_recall", "capture", "checkpoint", "consolidation", "searchability", "deletion"}
)


def load_manifest() -> tuple[dict[str, Any], bytes]:
    raw = resources.files("albert").joinpath("automatic_memory_release.json").read_bytes()
    return json.loads(raw), raw


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema_version") != 1:
        errors.append("unsupported schema_version")
    release = manifest.get("release", {})
    try:
        installed_version = metadata.version("albert-memory")
    except metadata.PackageNotFoundError:
        installed_version = release.get("version")
    if release.get("version") != installed_version:
        errors.append("manifest version does not match installed package")
    hosts = manifest.get("hosts", {})
    if set(hosts) != HOSTS:
        errors.append("host matrix must contain exactly the released adapters")
    for name, host in hosts.items():
        missing = GOLDEN_FLOW - set(host.get("golden_flow", []))
        if missing:
            errors.append(f"{name} lacks golden-flow evidence: {', '.join(sorted(missing))}")
        for field in ("tested", "supported", "fidelity", "fixture", "documentation"):
            if not host.get(field):
                errors.append(f"{name} lacks {field}")
    failed = [
        name for name, status in manifest.get("security_gates", {}).items() if status != "pass"
    ]
    if failed:
        errors.append(f"security gates are not passing: {', '.join(sorted(failed))}")
    blockers = manifest.get("beta", {}).get("unresolved_launch_blockers")
    if blockers is None or blockers:
        errors.append("beta has unresolved or unrecorded launch blockers")
    return errors


def receipt() -> dict[str, Any]:
    manifest, raw = load_manifest()
    errors = validate_manifest(manifest)
    return {
        "status": "PASS" if not errors else "FAIL",
        "package": manifest.get("release", {}).get("package"),
        "version": manifest.get("release", {}).get("version"),
        "channel": manifest.get("release", {}).get("channel"),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "hosts": sorted(manifest.get("hosts", {})),
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write the JSON receipt to this path")
    arguments = parser.parse_args()
    result = receipt()
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
