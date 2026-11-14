from __future__ import annotations

import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest
import typer

from albert import local_runtime


def _fake_bootstrap(root: Path, _pepper: str) -> str:
    with sqlite3.connect(root / local_runtime.DATABASE) as database:
        database.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        database.execute("INSERT INTO sentinel VALUES ('original')")
    return "alb_test_credential"


def _install(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(local_runtime, "_bootstrap_database", _fake_bootstrap)
    assert local_runtime.install_runtime(root, "basic", 18080, 18081)


def test_install_is_private_secret_safe_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runtime"
    _install(monkeypatch, root)

    assert not local_runtime.install_runtime(root, "basic", 18080, 18081)
    assert root.stat().st_mode & 0o777 == 0o700
    for name in (local_runtime.MARKER, local_runtime.CONFIG, local_runtime.SECRETS):
        assert (root / name).stat().st_mode & 0o777 == 0o600
    config = json.loads((root / local_runtime.CONFIG).read_text())
    private = json.loads((root / local_runtime.SECRETS).read_text())
    assert config["bind_host"] == "127.0.0.1"
    assert config["single_node"] is True
    assert "api_key" not in config
    assert private["api_key"] == "alb_test_credential"


def test_semantic_preflight_fails_before_state_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runtime"
    monkeypatch.setattr(local_runtime.importlib.util, "find_spec", lambda _name: None)

    with pytest.raises(typer.Exit):
        local_runtime.install_runtime(root, "semantic", 18080, 18081)

    assert not root.exists()


def test_backup_restore_round_trip_and_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runtime"
    _install(monkeypatch, root)
    archive = local_runtime.backup_runtime(root, tmp_path / "backup.zip")
    assert archive.stat().st_mode & 0o777 == 0o600

    with sqlite3.connect(root / local_runtime.DATABASE) as database:
        database.execute("UPDATE sentinel SET value = 'changed'")
        database.commit()
    local_runtime.restore_runtime(root, archive)
    with sqlite3.connect(root / local_runtime.DATABASE) as database:
        assert database.execute("SELECT value FROM sentinel").fetchone() == ("original",)

    corrupt = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(corrupt, "w") as target:
        for name in source.namelist():
            value = source.read(name)
            target.writestr(name, value + b"tampered" if name == local_runtime.CONFIG else value)
    os.chmod(corrupt, 0o600)
    with pytest.raises(typer.Exit):
        local_runtime.restore_runtime(root, corrupt)


def test_owned_root_rejects_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "runtime"
    _install(monkeypatch, root)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)

    with pytest.raises(typer.Exit):
        local_runtime._owned_root(alias)


def test_settings_reject_tampered_network_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runtime"
    _install(monkeypatch, root)
    config = json.loads((root / local_runtime.CONFIG).read_text())
    config["bind_host"] = "0.0.0.0"
    (root / local_runtime.CONFIG).write_text(json.dumps(config))
    os.chmod(root / local_runtime.CONFIG, 0o600)

    with pytest.raises(typer.Exit):
        local_runtime._settings(root)


def test_uninstall_preserves_by_default_and_remove_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runtime"
    _install(monkeypatch, root)
    monkeypatch.setattr(local_runtime, "stop_runtime", lambda _root: False)

    local_runtime.uninstall(root=root, remove_data=False)
    assert json.loads((root / local_runtime.CONFIG).read_text())["installed"] is False
    local_runtime.uninstall(root=root, remove_data=True)
    assert not root.exists()
    assert tmp_path.exists()
