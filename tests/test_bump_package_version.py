from __future__ import annotations

import runpy
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.version import Version

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "bump-package-version.py"
VERSION_FILES = tuple(runpy.run_path(str(SCRIPT))["VERSION_FILES"])


def current_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def copy_version_tree(target: Path) -> None:
    for relative in VERSION_FILES:
        source = ROOT / relative
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def test_bump_updates_every_release_coupled_version(tmp_path: Path) -> None:
    copy_version_tree(tmp_path)
    old = current_version()
    parsed = Version(old)
    new = f"{parsed.major}.{parsed.minor}.{parsed.micro + 1}"

    result = subprocess.run(
        [sys.executable, str(SCRIPT), new, "--root", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"from {old} to {new}" in result.stdout
    for relative in VERSION_FILES:
        text = (tmp_path / relative).read_text()
        assert old not in text, relative
        assert new in text, relative


def test_bump_rejects_non_normalized_or_older_versions(tmp_path: Path) -> None:
    copy_version_tree(tmp_path)

    non_normalized = subprocess.run(
        [sys.executable, str(SCRIPT), "0.04.1", "--root", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    older = subprocess.run(
        [sys.executable, str(SCRIPT), "0.0.1", "--root", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert non_normalized.returncode != 0
    assert "normalized PEP 440 form" in non_normalized.stderr
    assert older.returncode != 0
    assert "must be newer" in older.stderr


def test_bump_does_not_partially_edit_when_a_version_file_drifted(tmp_path: Path) -> None:
    copy_version_tree(tmp_path)
    old = current_version()
    parsed = Version(old)
    new = f"{parsed.major}.{parsed.minor}.{parsed.micro + 1}"
    drifted = tmp_path / VERSION_FILES[-1]
    drifted.write_text(drifted.read_text().replace(old, "9.9.9"))

    result = subprocess.run(
        [sys.executable, str(SCRIPT), new, "--root", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "does not contain current version" in result.stderr
    assert old in (tmp_path / VERSION_FILES[0]).read_text()
    assert new not in (tmp_path / VERSION_FILES[0]).read_text()
