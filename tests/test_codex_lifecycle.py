from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from albert.capture_installer import InstallError
from albert.codex_adapter import _client_from_environment
from albert.codex_lifecycle import (
    CodexInstallPaths,
    app,
    doctor_codex,
    install_codex,
    revoke_codex,
    uninstall_codex,
    unverified_warning,
)
from albert.host_targets import negotiate_host_target

ROOT = Path(__file__).parents[1]
HOOKS = ROOT / "src/albert/codex_hooks/hooks.json"


VERIFIED = json.loads((ROOT / "src/albert/host_targets/codex.json").read_text())["host"]["tested"]


def _newer(version: str) -> str:
    major, minor, _ = (int(part) for part in version.split("."))
    return f"{major}.{minor + 1}.0"


class CodexRunner:
    def __init__(self, version: str = VERIFIED) -> None:
        self.version = version
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command == ["codex", "--version"]:
            return subprocess.CompletedProcess(command, 0, f"codex-cli {self.version}\n", "")
        raise AssertionError(f"unexpected command: {command}")


def _paths(tmp_path: Path) -> CodexInstallPaths:
    return CodexInstallPaths(tmp_path / ".codex/albert", tmp_path / ".codex/hooks.json")


def _install(
    paths: CodexInstallPaths,
    runner: CodexRunner,
    token: str = "secret",
    brain_check=lambda *_: None,
) -> None:
    install_codex(
        api_url="https://brain.example",
        token=token,
        workspace_id="workspace-1",
        paths=paths,
        runner=runner,
        brain_check=brain_check,
        hook_source=HOOKS,
    )


def test_codex_exact_version_and_reduced_fidelity_are_truthful() -> None:
    target = negotiate_host_target("codex", (0, 166, 0), {"capture", "recall"})
    package = json.loads((ROOT / "src/albert/codex_package.json").read_text())
    assert target.hooks["fidelity"] == "reduced"
    assert target.hooks["absent"] == ["failure"]
    assert {"interrupt", "session_end"} <= set(target.hooks["present"])
    assert target.package["artifact_provenance"] == "github-oidc-dsse+sha256"
    assert package["package_version"] == target.package["version"] == "0.4.1"
    assert package["tested_codex"] == target.host["tested"]
    assert package["supported_codex"] == target.host["supported"]
    assert package["provenance"] == target.package["artifact_provenance"]


def test_install_doctor_rotate_revoke_and_uninstall_preserve_unrelated_hooks(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths.hooks_config.parent.mkdir(parents=True)
    unrelated = {
        "hooks": {
            "Stop": [{"matcher": "other", "hooks": [{"type": "command", "command": "other-hook"}]}]
        },
        "unrelated": True,
    }
    paths.hooks_config.write_text(json.dumps(unrelated))
    runner = CodexRunner()

    _install(paths, runner, "first-secret")
    first = json.loads(paths.credentials.read_text())
    configured = json.loads(paths.hooks_config.read_text())
    assert configured["unrelated"] is True
    assert configured["hooks"]["Stop"][0] == unrelated["hooks"]["Stop"][0]
    assert paths.credentials.stat().st_mode & 0o777 == 0o600
    assert paths.marker.stat().st_mode & 0o777 == 0o600
    assert paths.adapter.stat().st_mode & 0o777 == 0o700
    assert "first-secret" not in paths.hooks_config.read_text()
    assert "first-secret" not in json.dumps(runner.commands)

    _install(paths, runner, "replacement-secret")
    second = json.loads(paths.credentials.read_text())
    assert second["ALBERT_CAPTURE_QUEUE_KEY"] == first["ALBERT_CAPTURE_QUEUE_KEY"]
    assert second["ALBERT_CAPTURE_TOKEN"] == "replacement-secret"
    doctor_codex(paths, runner=runner, brain_check=lambda *_: None)
    revoke_codex(paths)
    assert not paths.credentials.exists()
    assert json.loads(paths.marker.read_text())["revoked"] is True
    uninstall_codex(paths, runner=runner)
    assert not paths.root.exists()
    assert json.loads(paths.hooks_config.read_text()) == unrelated


def test_failed_replacement_restores_config_and_exact_private_state(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    runner = CodexRunner()
    _install(paths, runner, "original-secret")
    credentials_before = paths.credentials.read_bytes()
    config_before = paths.hooks_config.read_bytes()
    calls = 0

    def fail_after_swap(*_):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise InstallError("replacement proof failed")

    with pytest.raises(InstallError, match="replacement proof failed"):
        _install(paths, runner, "failed-secret", fail_after_swap)

    assert paths.credentials.read_bytes() == credentials_before
    assert paths.hooks_config.read_bytes() == config_before
    assert not list(paths.root.parent.glob(".albert.*-*"))


def test_collisions_symlinks_and_unsupported_versions_fail_before_changes(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.hooks_config.parent.mkdir(parents=True)
    paths.hooks_config.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "/foreign/albert-codex-adapter"}]}
                    ]
                }
            }
        )
    )
    with pytest.raises(InstallError, match="foreign or stale"):
        _install(paths, CodexRunner())
    assert not paths.root.exists()

    paths.hooks_config.unlink()
    target = tmp_path / "foreign.json"
    target.write_text('{"hooks":{}}')
    paths.hooks_config.symlink_to(target)
    with pytest.raises(InstallError, match="symlink"):
        _install(paths, CodexRunner())
    assert not paths.root.exists()

    paths.hooks_config.unlink()
    with pytest.raises(InstallError, match="version is unsupported"):
        _install(paths, CodexRunner("0.165.0"))
    assert not paths.root.exists()


def test_adapter_reads_only_owned_private_credential_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = tmp_path / "credentials.json"
    credential.write_text(
        json.dumps(
            {
                "ALBERT_API_URL": "https://brain.example",
                "ALBERT_CAPTURE_QUEUE_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
                "ALBERT_CAPTURE_QUEUE_PATH": str(tmp_path / "capture.db"),
                "ALBERT_CAPTURE_TOKEN": "private-token",
                "ALBERT_WORKSPACE_ID": "workspace-1",
            }
        )
    )
    os.chmod(credential, 0o600)
    monkeypatch.setenv("ALBERT_CAPTURE_CREDENTIAL_FILE", str(credential))
    client, _, workspace_id = _client_from_environment()
    assert workspace_id == "workspace-1"
    client.close()

    os.chmod(credential, 0o644)
    with pytest.raises(RuntimeError, match="permissions are unsafe"):
        _client_from_environment()


def test_newer_codex_installs_with_unverified_warning_and_older_is_refused(
    tmp_path: Path,
) -> None:
    newer = _newer(VERIFIED)
    assert unverified_warning((0, 166, 0)) is None
    assert unverified_warning(tuple(int(p) for p in VERIFIED.split("."))) is None
    warning = unverified_warning(tuple(int(p) for p in newer.split(".")))
    assert warning is not None
    assert f"Codex {newer} is not yet verified with Albert" in warning
    assert VERIFIED in warning

    paths = _paths(tmp_path)
    runner = CodexRunner(newer)
    _install(paths, runner)
    marker = json.loads(paths.marker.read_text())
    assert marker["codex_version"] == newer
    assert doctor_codex(paths, runner=runner, brain_check=lambda *_: None) == tuple(
        int(p) for p in newer.split(".")
    )

    with pytest.raises(InstallError, match="version is unsupported"):
        _install(_paths(tmp_path / "old"), CodexRunner("0.165.9"))


def test_doctor_command_prints_unverified_warning_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    import albert.codex_lifecycle as lifecycle

    newer = tuple(int(p) for p in _newer(VERIFIED).split("."))
    monkeypatch.setattr(lifecycle, "doctor_codex", lambda *_a, **_k: newer)
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "not yet verified with Albert" in result.stderr
    assert "doctor passed" in result.stdout

    monkeypatch.setattr(
        lifecycle, "doctor_codex", lambda *_a, **_k: tuple(int(p) for p in VERIFIED.split("."))
    )
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "not yet verified" not in result.stderr
