from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts/set-verified-host-version.py"
spec = importlib.util.spec_from_file_location("set_verified_host_version", SCRIPT)
assert spec and spec.loader
bump = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bump)


def _copy_repo(tmp_path: Path) -> Path:
    for relative in {
        bump.RELEASE_MANIFEST,
        *(path for host in bump.HOSTS.values() for path in (host[0], host[1], *host[3])),
    }:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    return tmp_path


def _verified(root: Path, host: str = "codex") -> str:
    return json.loads((root / bump.HOSTS[host][0]).read_text())["host"]["tested"]


def _next_minor(version: str) -> str:
    major, minor, _ = (int(part) for part in version.split("."))
    return f"{major}.{minor + 1}.0"


def test_bump_moves_every_record_of_the_verified_codex_release(tmp_path: Path) -> None:
    root = _copy_repo(tmp_path)
    old = _verified(root)
    new = _next_minor(old)

    assert bump.main(["codex", new, "--root", str(root)]) == 0

    descriptor = json.loads((root / "src/albert/host_targets/codex.json").read_text())
    package = json.loads((root / "src/albert/codex_package.json").read_text())
    release = json.loads((root / bump.RELEASE_MANIFEST).read_text())
    assert descriptor["host"]["tested"] == package["tested_codex"] == new
    assert release["hosts"]["codex"]["tested"] == new
    assert release["hosts"]["claude-code"]["tested"] != new
    for doc in bump.HOSTS["codex"][3]:
        text = (root / doc).read_text()
        assert new in text and old not in text


def test_bump_refuses_downgrades_out_of_range_and_bad_input_without_changes(
    tmp_path: Path,
) -> None:
    root = _copy_repo(tmp_path)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    claude_old = _verified(root, "claude-code")
    major, minor, _ = (int(part) for part in claude_old.split("."))

    for args in (
        ["codex", "0.165.0"],
        ["codex", "latest"],
        ["claude-code", f"{major}.{minor + 1}.0"],  # outside <2.2.0
    ):
        assert bump.main([*args, "--root", str(root)]) == 1
    with pytest.raises(SystemExit):
        bump.main(["cursor", "1.0.0", "--root", str(root)])
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_bump_to_current_release_is_a_no_op(tmp_path: Path) -> None:
    root = _copy_repo(tmp_path)
    assert bump.plan(root, "codex", _verified(root)) == {}
