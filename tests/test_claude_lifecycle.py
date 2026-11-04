from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from albert.capture_installer import InstallError
from albert.claude_adapter import _client_from_environment
from albert.claude_lifecycle import (
    ClaudeInstallPaths,
    doctor_claude,
    install_claude,
    revoke_claude,
    uninstall_claude,
)
from albert.host_targets import negotiate_host_target

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "src/albert/claude_plugin"


class ClaudeRunner:
    def __init__(self, version: str = "2.1.312") -> None:
        self.version = version
        self.commands: list[list[str]] = []
        self.validation_count = 0
        self.fail_validation: int | None = None

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command == ["claude", "--version"]:
            return subprocess.CompletedProcess(command, 0, f"Claude Code {self.version}\n", "")
        if command[:4] == ["claude", "plugin", "validate", "--strict"]:
            self.validation_count += 1
            assert Path(command[4]).is_dir()
            if self.fail_validation == self.validation_count:
                return subprocess.CompletedProcess(command, 1, "", "invalid plugin")
            return subprocess.CompletedProcess(command, 0, "valid\n", "")
        raise AssertionError(f"unexpected command: {command}")


def _install(paths: ClaudeInstallPaths, runner: ClaudeRunner, token: str = "secret") -> None:
    install_claude(
        api_url="https://brain.example",
        token=token,
        workspace_id="workspace-1",
        paths=paths,
        runner=runner,
        brain_check=lambda *_: None,
        plugin_source=PLUGIN,
    )


def test_claude_exact_and_reduced_fidelity_versions_are_truthful() -> None:
    current = negotiate_host_target("claude-code", (2, 1, 312), {"capture", "recall"})
    reduced = negotiate_host_target("claude-code", (2, 1, 78), {"capture", "recall"})
    assert current.hooks == reduced.hooks
    assert current.hooks["fidelity"] == "reduced"
    assert current.hooks["absent"] == ["reliable_failure_delivery"]


def test_install_doctor_replace_revoke_and_uninstall_are_isolated(tmp_path: Path) -> None:
    paths = ClaudeInstallPaths(tmp_path / "albert")
    settings = tmp_path / "settings.json"
    settings.write_text('{"unrelated":true}\n')
    runner = ClaudeRunner()

    _install(paths, runner, "first-secret")
    first = json.loads(paths.credentials.read_text())
    assert paths.credentials.stat().st_mode & 0o777 == 0o600
    assert paths.marker.stat().st_mode & 0o777 == 0o600
    assert paths.adapter.stat().st_mode & 0o777 == 0o700
    hooks = json.loads((paths.plugin / "hooks/hooks.json").read_text())
    commands = [
        hook["command"]
        for matchers in hooks["hooks"].values()
        for matcher in matchers
        for hook in matcher["hooks"]
    ]
    assert commands == [str(paths.adapter)] * 6
    assert "first-secret" not in json.dumps(runner.commands)

    _install(paths, runner, "replacement-secret")
    second = json.loads(paths.credentials.read_text())
    assert second["ALBERT_CAPTURE_QUEUE_KEY"] == first["ALBERT_CAPTURE_QUEUE_KEY"]
    assert second["ALBERT_CAPTURE_TOKEN"] == "replacement-secret"
    doctor_claude(paths, runner=runner, brain_check=lambda *_: None)
    revoke_claude(paths)
    assert not paths.credentials.exists()
    assert json.loads(paths.marker.read_text())["revoked"] is True
    uninstall_claude(paths, runner=runner)
    assert not paths.root.exists()
    assert settings.read_text() == '{"unrelated":true}\n'


def test_failed_replacement_rolls_back_exact_private_state(tmp_path: Path) -> None:
    paths = ClaudeInstallPaths(tmp_path / "albert")
    runner = ClaudeRunner()
    _install(paths, runner, "original-secret")
    before = paths.credentials.read_bytes()
    runner.fail_validation = runner.validation_count + 2

    with pytest.raises(InstallError, match="installed plugin validation"):
        _install(paths, runner, "failed-secret")

    assert paths.credentials.read_bytes() == before
    assert not list(tmp_path.glob(".albert.*-*"))


def test_reconnect_after_revoke_preserves_queue_key_and_state(tmp_path: Path) -> None:
    paths = ClaudeInstallPaths(tmp_path / "albert")
    runner = ClaudeRunner()
    _install(paths, runner, "original-secret")
    original = json.loads(paths.credentials.read_text())
    queue_state = b"encrypted queue state"
    claude_state = b"fresh-session recall state"
    paths.queue.write_bytes(queue_state)
    (paths.root / "capture.claude-state.db").write_bytes(claude_state)

    revoke_claude(paths)
    revoked_marker = json.loads(paths.marker.read_text())
    assert revoked_marker["reconnect_queue_key"] == original["ALBERT_CAPTURE_QUEUE_KEY"]

    _install(paths, runner, "replacement-secret")

    reconnected = json.loads(paths.credentials.read_text())
    assert reconnected["ALBERT_CAPTURE_QUEUE_KEY"] == original["ALBERT_CAPTURE_QUEUE_KEY"]
    assert reconnected["ALBERT_CAPTURE_TOKEN"] == "replacement-secret"
    assert paths.queue.read_bytes() == queue_state
    assert (paths.root / "capture.claude-state.db").read_bytes() == claude_state
    assert "revoked" not in json.loads(paths.marker.read_text())
    assert "reconnect_queue_key" not in json.loads(paths.marker.read_text())


def test_malicious_plugin_fails_without_disturbing_existing_install(tmp_path: Path) -> None:
    paths = ClaudeInstallPaths(tmp_path / "albert")
    runner = ClaudeRunner()
    _install(paths, runner, "original-secret")
    before = paths.credentials.read_bytes()
    malicious = tmp_path / "malicious-plugin"
    malicious.mkdir()
    (malicious / "escape").symlink_to(paths.credentials)

    with pytest.raises(InstallError, match="must not contain symlinks"):
        install_claude(
            api_url="https://brain.example",
            token="replacement-secret",
            workspace_id=None,
            paths=paths,
            runner=runner,
            brain_check=lambda *_: None,
            plugin_source=malicious,
        )

    assert paths.credentials.read_bytes() == before


def test_unsupported_host_and_unsafe_existing_root_fail_before_changes(tmp_path: Path) -> None:
    paths = ClaudeInstallPaths(tmp_path / "albert")
    with pytest.raises(InstallError, match="version is unsupported"):
        _install(paths, ClaudeRunner("2.2.0"))
    assert not paths.root.exists()

    paths.root.mkdir()
    paths.marker.write_text('{"owner":"foreign"}\n')
    os.chmod(paths.marker, 0o600)
    with pytest.raises(InstallError, match="foreign ownership"):
        _install(paths, ClaudeRunner())
    assert json.loads(paths.marker.read_text())["owner"] == "foreign"


def test_claude_adapter_reads_only_owned_private_credential_file(
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
            }
        )
    )
    os.chmod(credential, 0o600)
    monkeypatch.setenv("ALBERT_CAPTURE_CREDENTIAL_FILE", str(credential))
    client, _, _ = _client_from_environment()
    client.close()

    os.chmod(credential, 0o644)
    with pytest.raises(RuntimeError, match="permissions are unsafe"):
        _client_from_environment()
