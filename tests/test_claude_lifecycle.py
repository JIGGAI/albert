from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from albert.capture_installer import InstallError
from albert.claude_adapter import _client_from_environment
from albert.claude_lifecycle import (
    ClaudeInstallPaths,
    connect_claude,
    doctor_claude,
    install_claude,
    revoke_claude,
    uninstall_claude,
)
from albert.host_targets import negotiate_host_target
from tests.test_capture_installer import CAPABILITIES, REST, cloud_contract, redeemed_response

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "src/albert/claude_plugin"


class ClaudeRunner:
    """A `claude` CLI double that keeps plugin and marketplace state in memory."""

    def __init__(self, version: str = "2.1.312", plugins: set[str] | None = None) -> None:
        self.version = version
        self.commands: list[list[str]] = []
        self.validation_count = 0
        self.fail_validation: int | None = None
        self.fail_install = False
        self.plugins: set[str] = set(plugins or ())
        self.marketplaces: dict[str, str] = {}

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        ok = subprocess.CompletedProcess(command, 0, "{}\n", "")
        if command == ["claude", "--version"]:
            return subprocess.CompletedProcess(command, 0, f"Claude Code {self.version}\n", "")
        if command[:4] == ["claude", "plugin", "validate", "--strict"]:
            self.validation_count += 1
            assert Path(command[4]).is_dir()
            if self.fail_validation == self.validation_count:
                return subprocess.CompletedProcess(command, 1, "", "invalid plugin")
            return subprocess.CompletedProcess(command, 0, "valid\n", "")
        if command == ["claude", "plugin", "list", "--json"]:
            listing = [{"enabled": True, "id": item} for item in sorted(self.plugins)]
            return subprocess.CompletedProcess(command, 0, json.dumps(listing), "")
        if command[:4] == ["claude", "plugin", "marketplace", "add"]:
            manifest = json.loads(
                (Path(command[4]) / ".claude-plugin" / "marketplace.json").read_text()
            )
            self.marketplaces[manifest["name"]] = command[4]
            return ok
        if command[:4] == ["claude", "plugin", "marketplace", "remove"]:
            self.marketplaces.pop(command[4], None)
            return ok
        if command[:3] == ["claude", "plugin", "install"]:
            if self.fail_install:
                return subprocess.CompletedProcess(command, 1, "", "install refused")
            assert command[3].split("@")[1] in self.marketplaces
            self.plugins.add(command[3])
            return ok
        if command[:3] == ["claude", "plugin", "update"]:
            assert command[3] in self.plugins
            return ok
        if command[:3] == ["claude", "plugin", "uninstall"]:
            self.plugins.discard(command[3])
            return ok
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
    # Plain `claude` loads it: no launcher, a local marketplace in the owned tree.
    assert runner.plugins == {"albert-memory@albert-local"}
    assert runner.marketplaces == {"albert-local": str(paths.marketplace)}
    assert not (paths.root / "bin" / "albert-claude").exists()
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
    assert runner.plugins == set() and runner.marketplaces == {}
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


def test_connect_redeems_a_claude_link_and_records_cloud_identity(tmp_path: Path) -> None:
    link, manifest, trust = cloud_contract(family="claude-code")
    redemptions: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/manifest/v2"):
            return httpx.Response(200, json=manifest)
        if request.url.path.endswith("/redeem/v2"):
            redemptions.append(json.loads(request.content))
            return redeemed_response("claude-credential")
        return httpx.Response(404)

    paths = ClaudeInstallPaths(tmp_path / "albert")
    runner = ClaudeRunner()
    connect_claude(
        link_url=link,
        paths=paths,
        runner=runner,
        trust=trust,
        package_version=manifest["adapter"]["package_version"],
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        brain_check=lambda *_: None,
        plugin_source=PLUGIN,
    )

    assert redemptions[0]["agent_name"] == "Claude Code"
    assert redemptions[0]["capabilities"] == CAPABILITIES
    values = json.loads(paths.credentials.read_text())
    assert values["ALBERT_CAPTURE_TOKEN"] == "claude-credential"
    assert values["ALBERT_API_URL"] == REST
    assert values["ALBERT_WORKSPACE_ID"] == "workspace-1"
    marker = json.loads(paths.marker.read_text())
    assert marker["cloud"]["credential_id"] == "credential-1"
    assert marker["cloud"]["package_version"] == manifest["adapter"]["package_version"]
    assert "claude-credential" not in paths.marker.read_text() + json.dumps(runner.commands)


def test_connect_refuses_a_link_made_for_another_host(tmp_path: Path) -> None:
    link, manifest, trust = cloud_contract(family="openclaw")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/manifest/v2"):
            return httpx.Response(200, json=manifest)
        raise AssertionError("an OpenClaw link must not be redeemed by Claude Code")

    paths = ClaudeInstallPaths(tmp_path / "albert")
    with pytest.raises(InstallError, match="provenance"):
        connect_claude(
            link_url=link,
            paths=paths,
            runner=ClaudeRunner(),
            trust=trust,
            package_version=manifest["adapter"]["package_version"],
            http=httpx.Client(transport=httpx.MockTransport(handler)),
            brain_check=lambda *_: None,
            plugin_source=PLUGIN,
        )
    assert not paths.root.exists()


def test_connect_compensates_the_credential_when_install_fails(tmp_path: Path) -> None:
    link, manifest, trust = cloud_contract(family="claude-code")
    compensated: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/manifest/v2"):
            return httpx.Response(200, json=manifest)
        if request.url.path.endswith("/redeem/v2"):
            return redeemed_response("doomed-credential", receipt="claude-receipt")
        if request.url.path.endswith("/onboard/compensate"):
            compensated.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "connection_id": "connection-1",
                    "credential_id": "credential-1",
                    "revoked": True,
                },
            )
        return httpx.Response(404)

    def failing_check(*_: object) -> None:
        raise InstallError("live capture smoke test was rejected")

    paths = ClaudeInstallPaths(tmp_path / "albert")
    with pytest.raises(InstallError, match="smoke"):
        connect_claude(
            link_url=link,
            paths=paths,
            runner=ClaudeRunner(),
            trust=trust,
            package_version=manifest["adapter"]["package_version"],
            http=httpx.Client(transport=httpx.MockTransport(handler)),
            brain_check=failing_check,
            plugin_source=PLUGIN,
        )
    assert compensated == [{"receipt": "claude-receipt"}]
    assert not paths.root.exists()


def test_connect_detects_an_unsupported_host_before_touching_the_link(tmp_path: Path) -> None:
    link, manifest, trust = cloud_contract(family="claude-code")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the link must not be fetched for an unsupported host")

    with pytest.raises(InstallError):
        connect_claude(
            link_url=link,
            paths=ClaudeInstallPaths(tmp_path / "albert"),
            runner=ClaudeRunner(version="2.0.1"),
            trust=trust,
            package_version=manifest["adapter"]["package_version"],
            http=httpx.Client(transport=httpx.MockTransport(handler)),
            brain_check=lambda *_: None,
            plugin_source=PLUGIN,
        )


def test_a_public_marketplace_install_is_kept_instead_of_a_second_copy(tmp_path: Path) -> None:
    paths = ClaudeInstallPaths(tmp_path / "albert")
    runner = ClaudeRunner(plugins={"albert-memory@albert"})

    _install(paths, runner)

    # One copy of the hooks: the public plugin reads the same credential file.
    assert runner.plugins == {"albert-memory@albert"}
    assert runner.marketplaces == {}
    assert paths.credentials.exists()
    doctor_claude(paths, runner=runner, brain_check=lambda *_: None)


def test_failed_plugin_install_leaves_no_registration_behind(tmp_path: Path) -> None:
    paths = ClaudeInstallPaths(tmp_path / "albert")
    runner = ClaudeRunner()
    runner.fail_install = True

    with pytest.raises(InstallError, match="Claude plugin install"):
        _install(paths, runner)

    assert runner.plugins == set() and runner.marketplaces == {}
    assert not paths.root.exists()


def test_failed_proof_after_registration_unregisters_a_fresh_install(tmp_path: Path) -> None:
    paths = ClaudeInstallPaths(tmp_path / "albert")
    runner = ClaudeRunner()
    calls = 0

    def second_check_fails(*_: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise InstallError("live capture smoke test was rejected")

    with pytest.raises(InstallError, match="smoke"):
        install_claude(
            api_url="https://brain.example",
            token="secret",
            workspace_id="workspace-1",
            paths=paths,
            runner=runner,
            brain_check=second_check_fails,
            plugin_source=PLUGIN,
        )
    assert runner.plugins == set() and runner.marketplaces == {}


def test_reinstall_keeps_an_existing_registration(tmp_path: Path) -> None:
    paths = ClaudeInstallPaths(tmp_path / "albert")
    runner = ClaudeRunner()
    _install(paths, runner, "first")
    runner.fail_validation = runner.validation_count + 2

    with pytest.raises(InstallError):
        _install(paths, runner, "second")

    # The previous install is restored and still registered.
    assert runner.plugins == {"albert-memory@albert-local"}
    assert json.loads(paths.credentials.read_text())["ALBERT_CAPTURE_TOKEN"] == "first"


def test_doctor_reports_a_plugin_removed_from_claude_code(tmp_path: Path) -> None:
    paths = ClaudeInstallPaths(tmp_path / "albert")
    runner = ClaudeRunner()
    _install(paths, runner)
    runner.plugins.clear()

    with pytest.raises(InstallError, match="not installed in Claude Code"):
        doctor_claude(paths, runner=runner, brain_check=lambda *_: None)


def test_local_marketplace_lists_only_the_owned_plugin(tmp_path: Path) -> None:
    paths = ClaudeInstallPaths(tmp_path / "albert")
    _install(paths, ClaudeRunner())

    manifest = json.loads((paths.marketplace / ".claude-plugin" / "marketplace.json").read_text())
    assert manifest["name"] == "albert-local"
    assert manifest["plugins"] == [
        {
            "description": "Albert automatic memory for Claude Code",
            "name": "albert-memory",
            "source": "./plugin",
        }
    ]
    mcp = json.loads((paths.plugin / ".mcp.json").read_text())["mcpServers"]["albert"]
    assert mcp["env"]["ALBERT_MCP_CREDENTIAL_FILE"] == "${HOME}/.claude/albert/credentials.env"


def test_marketplace_hooks_find_the_installer_credential_file_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = tmp_path / "credentials.env"
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
    monkeypatch.delenv("ALBERT_CAPTURE_CREDENTIAL_FILE", raising=False)
    monkeypatch.delenv("ALBERT_CAPTURE_TOKEN", raising=False)
    monkeypatch.setattr("albert.claude_adapter.DEFAULT_CREDENTIAL_FILE", credential)

    client, _, _ = _client_from_environment()
    assert client.client.headers["Authorization"] == "Bearer private-token"
    client.close()
