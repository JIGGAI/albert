#!/usr/bin/env python3
"""Move a host adapter's verified release in every file that records it.

    scripts/set-verified-host-version.py codex 0.168.0

The verified ("tested") release lives in the host-target descriptor and is mirrored
in the package manifest, the automatic-memory release manifest, and the docs.
The development team's host-compatibility check runs this after the conformance
suite passes on a new host release, then opens a pull request with the result.
It refuses versions outside the declared supported range or older than the
current verified release, and changes nothing unless every file updates.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"\d+\.\d+\.\d+")

# host -> (descriptor, package manifest, package field, docs that quote the version)
HOSTS = {
    "codex": (
        "src/albert/host_targets/codex.json",
        "src/albert/codex_package.json",
        "tested_codex",
        ("docs/integrations/codex.md", "docs/AUTOMATIC_MEMORY_RELEASE.md"),
    ),
    "claude-code": (
        "src/albert/host_targets/claude-code.json",
        "src/albert/claude_package.json",
        "tested_claude",
        ("docs/AUTOMATIC_MEMORY_RELEASE.md",),
    ),
}
RELEASE_MANIFEST = "src/albert/automatic_memory_release.json"


def _parse(version: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in version.split("."))  # type: ignore[return-value]


def _in_range(version: tuple[int, int, int], declared: str) -> bool:
    sys.path.insert(0, str(ROOT / "src"))
    from albert.host_targets import _version_in_range

    return _version_in_range(version, declared)


def plan(root: Path, host: str, new: str) -> dict[Path, str]:
    """Return the new text of every file to change, or raise ValueError."""
    if host not in HOSTS:
        raise ValueError(f"unknown host {host!r}; known: {', '.join(sorted(HOSTS))}")
    if not VERSION.fullmatch(new):
        raise ValueError(f"version must be MAJOR.MINOR.PATCH, got {new!r}")
    descriptor_path, package_path, package_field, docs = HOSTS[host]
    descriptor = json.loads((root / descriptor_path).read_text(encoding="utf-8"))
    old = descriptor["host"]["tested"]
    if _parse(new) < _parse(old):
        raise ValueError(f"{new} is older than the verified release {old}")
    if not _in_range(_parse(new), descriptor["host"]["supported"]):
        raise ValueError(f"{new} is outside the supported range {descriptor['host']['supported']}")
    if new == old:
        return {}

    changes: dict[Path, str] = {}

    def replace_exact(relative: str, before: str, after: str, *, required: bool = True) -> None:
        path = root / relative
        text = changes.get(path, path.read_text(encoding="utf-8"))
        if before not in text:
            if required:
                raise ValueError(f"{relative} does not contain {before!r}")
            return
        changes[path] = text.replace(before, after)

    replace_exact(descriptor_path, f'"tested": "{old}"', f'"tested": "{new}"')
    replace_exact(package_path, f'"{package_field}": "{old}"', f'"{package_field}": "{new}"')
    replace_exact(
        RELEASE_MANIFEST, f'"{host}": {{"tested": "{old}"', f'"{host}": {{"tested": "{new}"'
    )
    for relative in docs:
        replace_exact(relative, old, new)
    for path, text in changes.items():
        if path.suffix == ".json":
            json.loads(text)
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("host", choices=sorted(HOSTS))
    parser.add_argument("version")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    try:
        changes = plan(arguments.root, arguments.host, arguments.version)
    except (OSError, KeyError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    for path, text in changes.items():
        path.write_text(text, encoding="utf-8")
        print(path.relative_to(arguments.root))
    if not changes:
        print(f"{arguments.host} is already verified at {arguments.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
