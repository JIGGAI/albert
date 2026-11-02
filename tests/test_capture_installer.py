from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from albert.capture_installer import (
    InstallError,
    InstallPaths,
    _brain_check,
    _credentials_text,
    _detect_openclaw,
    install_openclaw,
    uninstall_openclaw,
)


class FakeRunner:
    def __init__(self, config: Path, *, fail_on: tuple[str, ...] | None = None) -> None:
        self.config = config
        self.fail_on = fail_on
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command == ["openclaw", "--version"]:
            return subprocess.CompletedProcess(command, 0, "OpenClaw 2026.9.6 (test)\n", "")
        if command == ["openclaw", "config", "file"]:
            return subprocess.CompletedProcess(command, 0, f"{self.config}\n", "")
        if command[1:3] == ["config", "set"]:
            self.config.write_text('{"changed":true}\n', encoding="utf-8")
        if self.fail_on and tuple(command[1 : 1 + len(self.fail_on)]) == self.fail_on:
            return subprocess.CompletedProcess(command, 1, "", "injected failure\n")
        return subprocess.CompletedProcess(command, 0, "{}\n", "")


def install(paths: InstallPaths, runner: FakeRunner, token: str = "secret token") -> None:
    install_openclaw(
        api_url="https://brain.example",
        token=token,
        workspace_id="workspace-1",
        paths=paths,
        runner=runner,
        brain_check=lambda *_: None,
    )


def test_install_is_private_and_never_places_credentials_in_commands(tmp_path: Path):
    config = tmp_path / "openclaw.json"
    config.write_text('{"unrelated":true}\n', encoding="utf-8")
    paths = InstallPaths(tmp_path / "albert")
    runner = FakeRunner(config)

    install(paths, runner)

    assert paths.marker.exists() and paths.wrapper.exists()
    assert stat_mode(paths.credentials) == 0o600
    assert stat_mode(paths.wrapper) == 0o700
    assert all(
        "secret token" not in argument for command in runner.commands for argument in command
    )
    assert json.loads(paths.credentials.read_text())["ALBERT_CAPTURE_TOKEN"] == "secret token"
    entry_command = next(
        command for command in runner.commands if command[1:3] == ["config", "set"]
    )
    entry = json.loads(entry_command[4])
    assert entry["config"]["command"] == str(paths.wrapper)
    assert entry["hooks"] == {
        "allowPromptInjection": True,
        "allowConversationAccess": True,
    }


def test_repeat_install_is_idempotent_and_replaces_only_owned_tree(tmp_path: Path):
    config = tmp_path / "openclaw.json"
    config.write_text("{}\n", encoding="utf-8")
    paths = InstallPaths(tmp_path / "albert")
    runner = FakeRunner(config)

    install(paths, runner, "first")
    install(paths, runner, "second")

    assert json.loads(paths.credentials.read_text())["ALBERT_CAPTURE_TOKEN"] == "second"
    assert not list(tmp_path.glob(".albert.rollback-*"))
    assert not list(tmp_path.glob(".albert.staging-*"))


def test_partial_failure_restores_config_and_previous_install(tmp_path: Path):
    config = tmp_path / "openclaw.json"
    original_config = '{"unrelated":{"keep":true}}\n'
    config.write_text(original_config, encoding="utf-8")
    paths = InstallPaths(tmp_path / "albert")
    first = FakeRunner(config)
    install(paths, first, "original-secret")
    previous_credentials = paths.credentials.read_bytes()
    config.write_text(original_config, encoding="utf-8")
    failing = FakeRunner(config, fail_on=("plugins", "inspect"))

    with pytest.raises(InstallError, match="runtime inspection"):
        install(paths, failing, "replacement-secret")

    assert config.read_text() == original_config
    assert paths.credentials.read_bytes() == previous_credentials
    assert not any(command[1:3] == ["plugins", "uninstall"] for command in failing.commands)
    assert not list(tmp_path.glob(".albert.rollback-*"))
    assert not list(tmp_path.glob(".albert.staging-*"))


def test_fresh_install_failure_after_plugin_install_uninstalls_plugin(tmp_path: Path):
    config = tmp_path / "openclaw.json"
    original_config = '{"unrelated":{"keep":true}}\n'
    config.write_text(original_config, encoding="utf-8")
    paths = InstallPaths(tmp_path / "albert")
    runner = FakeRunner(config, fail_on=("config", "set"))

    with pytest.raises(InstallError, match="plugin configuration"):
        install(paths, runner)

    assert ["openclaw", "plugins", "uninstall", "albert-memory", "--force"] in runner.commands
    assert config.read_text() == original_config
    assert not paths.root.exists()
    assert not list(tmp_path.glob(".albert.rollback-*"))
    assert not list(tmp_path.glob(".albert.staging-*"))


def test_failed_plugin_rollback_prints_manual_cleanup_instructions(tmp_path: Path):
    config = tmp_path / "openclaw.json"
    config.write_text("{}\n", encoding="utf-8")
    paths = InstallPaths(tmp_path / "albert")

    class CleanupFailingRunner(FakeRunner):
        def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
            if command[1:3] == ["plugins", "uninstall"]:
                self.commands.append(command)
                return subprocess.CompletedProcess(command, 1, "", "cleanup denied\n")
            return super().__call__(command)

    runner = CleanupFailingRunner(config, fail_on=("config", "set"))

    with pytest.raises(InstallError, match=r"Run `openclaw plugins uninstall albert-memory"):
        install(paths, runner)

    assert config.read_text() == "{}\n"
    assert not paths.root.exists()


def test_uninstall_requires_ownership_and_removes_only_owned_tree(tmp_path: Path):
    config = tmp_path / "openclaw.json"
    config.write_text('{"unrelated":{"keep":true}}\n', encoding="utf-8")
    unrelated = tmp_path / "other-client"
    unrelated.mkdir()
    (unrelated / "settings.json").write_text("keep", encoding="utf-8")
    paths = InstallPaths(tmp_path / "albert")
    runner = FakeRunner(config)
    install(paths, runner)

    uninstall_openclaw(paths, runner=runner)

    assert not paths.root.exists()
    assert (unrelated / "settings.json").read_text() == "keep"
    uninstall = next(
        command for command in runner.commands if command[1:3] == ["plugins", "uninstall"]
    )
    assert uninstall == ["openclaw", "plugins", "uninstall", "albert-memory", "--force"]


def test_unsupported_versions_are_reported_truthfully(tmp_path: Path):
    def old_version(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "OpenClaw 2026.8.1\n", "")

    with pytest.raises(InstallError, match="unsupported"):
        _detect_openclaw(old_version)


def test_credentials_reject_newlines_and_encode_metacharacters(tmp_path: Path):
    text = _credentials_text(
        "https://brain.example", "value with $shell", "queue-key", tmp_path / "capture.db", None
    )
    assert json.loads(text)["ALBERT_CAPTURE_TOKEN"] == "value with $shell"
    config = tmp_path / "openclaw.json"
    config.write_text("{}", encoding="utf-8")
    with pytest.raises(InstallError, match="control characters"):
        install(InstallPaths(tmp_path / "albert"), FakeRunner(config), "bad\nsecret")


def test_direct_brain_check_runs_health_capability_capture_and_recall(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer hidden-token"
        if request.url.path == "/v1/health/ready":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200,
                json={
                    "paths": {
                        "/v1/capture/sessions": {},
                        "/v1/capture/sessions/{session_id}/events": {},
                        "/v1/context/session-start": {},
                    }
                },
            )
        if request.url.path == "/v1/capture/sessions":
            return httpx.Response(201, json={"id": "server-session"})
        if request.url.path == "/v1/capture/sessions/server-session/events":
            return httpx.Response(201, json={"id": "event-1"})
        if request.url.path == "/v1/context/session-start":
            return httpx.Response(200, json={"context": "smoke", "citations": []})
        if request.url.path == "/v1/capture/sessions/server-session/end":
            return httpx.Response(200, json={"id": "server-session"})
        return httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr(
        "albert.capture_installer.httpx.Client",
        lambda **kwargs: real_client(
            base_url=kwargs["base_url"],
            headers=kwargs["headers"],
            timeout=kwargs["timeout"],
            transport=httpx.MockTransport(handler),
        ),
    )

    _brain_check(
        "https://brain.example",
        "hidden-token",
        "X6j5KuOlKQzFJ2-rvRFNUAzkp_Uvf2VaxqwiirvOJ1o=",
        "workspace-1",
        smoke=True,
    )

    assert [request.url.path for request in requests] == [
        "/v1/health/ready",
        "/openapi.json",
        "/v1/capture/sessions",
        "/v1/capture/sessions/server-session/events",
        "/v1/context/session-start",
        "/v1/capture/sessions/server-session/end",
    ]


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
