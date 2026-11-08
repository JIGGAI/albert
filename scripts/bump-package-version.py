#!/usr/bin/env python3
"""Update every source-controlled albert-memory package-version reference."""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

from packaging.version import InvalidVersion, Version

VERSION_FILES = (
    "README.md",
    "docs/AUTOMATIC_MEMORY_RELEASE.md",
    "docs/DEPLOYMENT.md",
    "docs/integrations/codex.md",
    "docs/integrations/openclaw.md",
    "pyproject.toml",
    "skills/albert-connect/SKILL.md",
    "src/albert/__init__.py",
    "src/albert/automatic_memory_release.json",
    "src/albert/capture_installer.py",
    "src/albert/claude_package.json",
    "src/albert/claude_plugin/.claude-plugin/plugin.json",
    "src/albert/codex_package.json",
    "src/albert/host_targets/claude-code.json",
    "src/albert/host_targets/codex.json",
    "src/albert/host_targets/openclaw.json",
    "src/albert/openclaw_package.json",
    "src/albert/openclaw_plugin/package.json",
    "tests/test_albert_connect_skill.py",
    "tests/test_capture_installer.py",
    "tests/test_codex_lifecycle.py",
    "tests/test_openclaw_package_contract.py",
    "tests/test_release_bundle.py",
)


def fail(message: str) -> None:
    raise SystemExit(f"Package version bump failed: {message}")


def project_version(root: Path) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    if project.get("project", {}).get("name") != "albert-memory":
        fail("pyproject.toml is not the albert-memory project")
    version = project["project"].get("version")
    if not isinstance(version, str):
        fail("pyproject.toml has no string project.version")
    return version


def normalized_version(raw: str) -> Version:
    try:
        version = Version(raw)
    except InvalidVersion as exc:
        fail(f"{raw!r} is not a valid PEP 440 version: {exc}")
    if raw != str(version):
        fail(f"version must use normalized PEP 440 form: {version}")
    return version


def bump(root: Path, requested: str) -> tuple[str, str]:
    current = project_version(root)
    current_version = normalized_version(current)
    requested_version = normalized_version(requested)
    if requested_version <= current_version:
        fail(f"requested version {requested} must be newer than {current}")

    missing = [relative for relative in VERSION_FILES if not (root / relative).is_file()]
    if missing:
        fail(f"version files are missing: {', '.join(missing)}")

    updates: dict[str, str] = {}
    for relative in VERSION_FILES:
        path = root / relative
        text = path.read_text(encoding="utf-8")
        if current not in text:
            fail(f"{relative} does not contain current version {current}")
        updated = text.replace(current, requested)
        if current in updated:
            fail(f"{relative} still contains current version {current}")
        updates[relative] = updated

    for relative, updated in updates.items():
        (root / relative).write_text(updated, encoding="utf-8")

    if project_version(root) != requested:
        fail("pyproject.toml did not retain the requested version")
    return current, requested


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", help="new normalized PEP 440 package version")
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    previous, current = bump(args.root.resolve(), args.version)
    print(f"Updated albert-memory package version from {previous} to {current}")


if __name__ == "__main__":
    main()
