from __future__ import annotations

import json
import os
import subprocess
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from albert.capture_installer import (
    InstallError,
    InstallPaths,
    _brain_check,
    _credentials_text,
    _detect_openclaw,
    _https_url,
    _verify_manifest,
    connect_openclaw,
    install_openclaw,
    uninstall_openclaw,
)
from albert.host_targets import HostTargetError


def encoded(value: bytes) -> str:
    return urlsafe_b64encode(value).decode().rstrip("=")


def cloud_contract(
    *, authorization: str = "Bearer"
) -> tuple[str, dict[str, object], dict[str, object]]:
    link = (
        "https://console.example.com/api/brain-gateway/brain-1/rest/"
        "onboard/abcdefghijklmnopqrstuvwxyz123456?adapter_context=signed-context"
    )
    rest = "https://console.example.com/api/brain-gateway/brain-1/rest"
    adapter = {
        "artifact_sha256": "a" * 64,
        "artifact_signature": f"github-oidc-dsse:sha256:{'a' * 64}",
        "artifact_url": "https://releases.example.com/albert-memory-0.4.1.whl",
        "family": "openclaw",
        "package": "albert-memory",
        "package_version": "0.4.1",
        "supported_openclaw": ">=2026.9.6,<2027.0.0",
    }
    now = datetime.now(UTC)
    manifest: dict[str, object] = {
        "adapter": adapter,
        "capabilities": ["memory.read", "memory.write"],
        "compensation": {
            "credential_specific": True,
            "method": "POST",
            "receipt_ttl_seconds": 600,
            "url": f"{rest}/onboard/compensate",
        },
        "connection": {"authorization": authorization, "rest_url": rest},
        "expires_at": (now + timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
        "identity": {
            "brain_id": "brain-1",
            "cloud": "albert-cloud",
            "request_id": "request-1",
            "workspace_id": "workspace-1",
        },
        "issued_at": (now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        "manifest_id": "request-1",
        "manifest_version": "2.0",
        "redemption": {
            "identity_bound": True,
            "method": "POST",
            "single_use": True,
            "url": (
                f"{rest}/onboard/abcdefghijklmnopqrstuvwxyz123456/redeem/v2"
                "?adapter_context=signed-context"
            ),
        },
        "reload": {"command": ["openclaw", "plugins", "reload", "albert-memory"]},
        "revocation": {
            "credential_id_issued_on_redemption": True,
            "independent_agent": True,
            "scope": "single-credential",
        },
        "rollback": {
            "compensate_before_commit": True,
            "ownership_marker": "albert-memory",
            "restore_previous_state": True,
        },
        "schema": "com.jiggai.albert.adapter-manifest",
        "verification": {
            "capture_recall_smoke": True,
            "health": "/v1/health/ready",
            "runtime_inspection": True,
        },
    }
    private_key = Ed25519PrivateKey.generate()
    signature = encoded(
        private_key.sign(json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode())
    )
    manifest["signature"] = {
        "algorithm": "Ed25519",
        "key_id": "test-key",
        "value": signature,
    }
    public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    trust = {
        "manifest_keys": {"test-key": encoded(public)},
    }
    return link, manifest, trust


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
    assert json.loads(paths.marker.read_text())["package_version"] == "0.4.1"
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
    first_values = json.loads(paths.credentials.read_text())
    paths.queue.write_bytes(b"encrypted queued capture")
    install(paths, runner, "second")

    second_values = json.loads(paths.credentials.read_text())
    assert second_values["ALBERT_CAPTURE_TOKEN"] == "second"
    assert second_values["ALBERT_CAPTURE_QUEUE_KEY"] == first_values["ALBERT_CAPTURE_QUEUE_KEY"]
    assert paths.queue.read_bytes() == b"encrypted queued capture"
    assert stat_mode(paths.queue) == 0o600
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


def test_invalid_host_target_fails_before_filesystem_or_cloud_changes(tmp_path: Path, monkeypatch):
    config = tmp_path / "openclaw.json"
    config.write_text("{}\n", encoding="utf-8")
    paths = InstallPaths(tmp_path / "albert")
    runner = FakeRunner(config)
    monkeypatch.setattr(
        "albert.capture_installer.negotiate_host_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(HostTargetError("invalid target")),
    )

    with pytest.raises(InstallError, match="invalid target"):
        install(paths, runner)

    assert runner.commands == [["openclaw", "--version"]]
    assert config.read_text(encoding="utf-8") == "{}\n"
    assert not paths.root.exists()


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


def test_signed_cloud_connect_redeems_in_memory_and_persists_only_safe_metadata(
    tmp_path: Path, monkeypatch
):
    link, manifest, trust = cloud_contract()
    requests: list[httpx.Request] = []
    credential = "credential-canary-never-log"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/manifest/v2"):
            return httpx.Response(200, json=manifest)
        if request.url.path.endswith("/redeem/v2"):
            assert json.loads(request.content) == {
                "agent_name": "Native OpenClaw",
                "brain_id": "brain-1",
                "capabilities": ["memory.read", "memory.write"],
                "manifest_id": "request-1",
                "manifest_version": "2.0",
                "request_id": "request-1",
                "workspace_id": "workspace-1",
            }
            return httpx.Response(
                201,
                json={
                    "capabilities": ["memory.read", "memory.write"],
                    "compensation": {
                        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                        "receipt": "private-compensation-receipt",
                    },
                    "credential": credential,
                    "credential_notice": (
                        "Shown once; store only in the Albert-owned credential file."
                    ),
                    "identity": {
                        "brain_id": "brain-1",
                        "connection_id": "connection-1",
                        "credential_id": "credential-1",
                        "request_id": "request-1",
                        "workspace_id": "workspace-1",
                    },
                    "manifest_version": "2.0",
                    "rest_url": ("https://console.example.com/api/brain-gateway/brain-1/rest"),
                },
            )
        return httpx.Response(404)

    monkeypatch.setattr("albert.capture_installer._brain_check", lambda *args, **kwargs: None)
    config = tmp_path / "openclaw.json"
    config.write_text('{"unrelated":true}\n', encoding="utf-8")
    runner = FakeRunner(config)
    paths = InstallPaths(tmp_path / "albert")
    client = httpx.Client(transport=httpx.MockTransport(handler))

    connect_openclaw(
        link_url=link,
        paths=paths,
        runner=runner,
        trust=trust,
        package_version="0.4.1",
        http=client,
    )

    values = json.loads(paths.credentials.read_text())
    marker = json.loads(paths.marker.read_text())
    assert values["ALBERT_CAPTURE_TOKEN"] == credential
    assert marker["package_version"] == "0.4.1"
    assert marker["cloud"] == {
        "brain_id": "brain-1",
        "connection_id": "connection-1",
        "credential_id": "credential-1",
        "manifest_id": "request-1",
        "manifest_version": "2.0",
        "request_id": "request-1",
        "rest_url": "https://console.example.com/api/brain-gateway/brain-1/rest",
        "workspace_id": "workspace-1",
    }
    observable = json.dumps(runner.commands) + config.read_text() + paths.marker.read_text()
    assert credential not in observable
    assert credential not in os.environ.values()
    assert "private-compensation-receipt" not in observable
    assert link not in observable
    assert ["openclaw", "plugins", "reload", "albert-memory"] in runner.commands
    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == ["v2", "v2"]


def test_connect_failure_restores_predecessor_and_compensates_only_replacement(
    tmp_path: Path, monkeypatch
):
    link, manifest, trust = cloud_contract()
    compensated: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/manifest/v2"):
            return httpx.Response(200, json=manifest)
        if request.url.path.endswith("/redeem/v2"):
            return httpx.Response(
                201,
                json={
                    "capabilities": ["memory.read", "memory.write"],
                    "compensation": {
                        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                        "receipt": "replacement-only-receipt",
                    },
                    "credential": "replacement-secret",
                    "credential_notice": (
                        "Shown once; store only in the Albert-owned credential file."
                    ),
                    "identity": {
                        "brain_id": "brain-1",
                        "connection_id": "replacement-connection",
                        "credential_id": "replacement-credential",
                        "request_id": "request-1",
                        "workspace_id": "workspace-1",
                    },
                    "manifest_version": "2.0",
                    "rest_url": ("https://console.example.com/api/brain-gateway/brain-1/rest"),
                },
            )
        if request.url.path.endswith("/onboard/compensate"):
            compensated.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "connection_id": "replacement-connection",
                    "credential_id": "replacement-credential",
                    "revoked": True,
                },
            )
        return httpx.Response(404)

    monkeypatch.setattr("albert.capture_installer._brain_check", lambda *args, **kwargs: None)
    config = tmp_path / "openclaw.json"
    config.write_text("{}\n", encoding="utf-8")
    paths = InstallPaths(tmp_path / "albert")
    install(paths, FakeRunner(config), "predecessor-secret")
    previous = paths.credentials.read_bytes()
    failing = FakeRunner(config, fail_on=("plugins", "inspect"))

    with pytest.raises(InstallError, match="runtime inspection"):
        connect_openclaw(
            link_url=link,
            paths=paths,
            runner=failing,
            trust=trust,
            package_version="0.4.1",
            http=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    assert paths.credentials.read_bytes() == previous
    assert compensated == [{"receipt": "replacement-only-receipt"}]
    assert not any(command[1:3] == ["plugins", "uninstall"] for command in failing.commands)


def test_manifest_rejects_tampering_and_untrusted_release():
    link, manifest, trust = cloud_contract()
    manifest["capabilities"] = ["memory.read"]

    with pytest.raises(InstallError, match="signature is invalid"):
        _verify_manifest(
            manifest,
            link_url=link,
            openclaw_version=(2026, 9, 6),
            trust=trust,
            package_version="0.4.1",
        )

    _, clean_manifest, clean_trust = cloud_contract()
    with pytest.raises(InstallError, match="provenance"):
        _verify_manifest(
            clean_manifest,
            link_url=link,
            openclaw_version=(2026, 9, 6),
            trust=clean_trust,
            # Keep this deliberately unrelated to release versions so the
            # package-version bump script cannot make the mismatch valid.
            package_version="9999.0.0",
        )

    for endpoint in (
        "http://console.example.com/adapter",
        "https://127.0.0.1/adapter",
        "https://brain.internal/adapter",
        "https://user:password@console.example.com/adapter",
    ):
        with pytest.raises(InstallError, match="public HTTPS"):
            _https_url(endpoint, "Adapter endpoint")


def test_manifest_requires_bearer_authorization_scheme():
    link, manifest, trust = cloud_contract(authorization="Basic")

    with pytest.raises(InstallError, match="endpoint binding"):
        _verify_manifest(
            manifest,
            link_url=link,
            openclaw_version=(2026, 9, 6),
            trust=trust,
            package_version="0.4.1",
        )


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


# --- albert-capture classifier-agent -------------------------------------------


class AgentRunner:
    """OpenClaw CLI double for the classifier-agent command.

    Keeps a JSON config file like the real `openclaw config set` would, so the
    command's index lookups and the rollback can be checked against a file.
    """

    def __init__(
        self,
        config: Path,
        *,
        agents: tuple[str, ...] = ("main",),
        fail_on=None,
        version: str = "2026.9.6",
    ) -> None:
        self.config = config
        self.fail_on = fail_on
        self.version = version
        self.commands: list[list[str]] = []
        self.deleted: list[str] = []
        config.write_text(
            json.dumps(
                {
                    "agents": {
                        "defaults": {"model": {"primary": "openai/test-model"}},
                        "list": [{"id": agent, "workspace": f"/ws/{agent}"} for agent in agents],
                    },
                    "gateway": {"port": 18789, "auth": {"mode": "token", "token": "gw-secret"}},
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def _load(self) -> dict:
        return json.loads(self.config.read_text(encoding="utf-8"))

    def _save(self, data: dict) -> None:
        self.config.write_text(json.dumps(data) + "\n", encoding="utf-8")

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if self.fail_on and tuple(command[1 : 1 + len(self.fail_on)]) == self.fail_on:
            return subprocess.CompletedProcess(command, 1, "", "injected failure\n")
        if command == ["openclaw", "--version"]:
            return subprocess.CompletedProcess(command, 0, f"OpenClaw {self.version} (test)\n", "")
        if command == ["openclaw", "config", "file"]:
            return subprocess.CompletedProcess(command, 0, f"{self.config}\n", "")
        if command[1:3] == ["agents", "add"]:
            data = self._load()
            data["agents"]["list"].append(
                {"id": command[3], "workspace": command[command.index("--workspace") + 1]}
            )
            self._save(data)
            return subprocess.CompletedProcess(command, 0, f'{{"id":"{command[3]}"}}\n', "")
        if command[1:3] == ["agents", "delete"]:
            data = self._load()
            data["agents"]["list"] = [a for a in data["agents"]["list"] if a["id"] != command[3]]
            self._save(data)
            self.deleted.append(command[3])
            return subprocess.CompletedProcess(command, 0, "{}\n", "")
        if command[1:3] == ["config", "set"]:
            data = self._load()
            node: object = data
            parts = command[3].split(".")
            for part in parts[:-1]:
                node = node[int(part)] if isinstance(node, list) else node.setdefault(part, {})
            value = json.loads(command[4])
            if isinstance(node, list):
                node[int(parts[-1])] = value
            else:
                node[parts[-1]] = value
            self._save(data)
        return subprocess.CompletedProcess(command, 0, "{}\n", "")


def test_classifier_agent_install_adds_an_owned_agent_and_pins_its_runtime(tmp_path: Path):
    from albert.capture_installer import install_classifier_agent

    config = tmp_path / "openclaw.json"
    runner = AgentRunner(config)
    workspace = tmp_path / "workspace-albert-classifier"
    probes: list[tuple[str, str, str]] = []

    install_classifier_agent(
        agent_id="albert-classifier",
        workspace=workspace,
        model=None,
        runner=runner,
        probe=lambda url, token, agent: probes.append((url, token, agent)),
    )

    data = json.loads(config.read_text(encoding="utf-8"))
    agent = next(a for a in data["agents"]["list"] if a["id"] == "albert-classifier")
    assert agent["workspace"] == str(workspace)
    # No plugin tools (so no memory access) and no messaging; `session_status`
    # stays because OpenClaw refuses to run an agent with no callable tool at all.
    assert agent["tools"] == {"profile": "minimal", "deny": ["group:plugins", "message"]}
    # Default model comes from agents.defaults; the OpenClaw runtime pin makes the
    # Responses endpoint honour per-request instructions for an openai/* model.
    assert agent["models"] == {"openai/test-model": {"agentRuntime": {"id": "openclaw"}}}
    assert agent["identity"] == {"name": "Albert classifier"}
    assert data["gateway"]["http"]["endpoints"]["responses"]["enabled"] is True
    assert ["openclaw", "config", "validate"] in runner.commands
    # Workspace is Albert-owned and says what the agent is for.
    assert (workspace / "AGENTS.md").read_text(encoding="utf-8").startswith("# AGENTS.md")
    assert json.loads((workspace / ".albert-owned.json").read_text(encoding="utf-8"))["owner"] == (
        "albert-memory"
    )
    assert "openclaw/albert-classifier" not in (workspace / "SOUL.md").read_text()
    # The live probe used the gateway token from the config file, read in-process.
    assert probes == [("http://127.0.0.1:18789", "gw-secret", "albert-classifier")]
    # The token itself never appears on a command line.
    assert all("gw-secret" not in " ".join(command) for command in runner.commands)


def test_classifier_agent_install_is_idempotent_and_honours_model_override(tmp_path: Path):
    from albert.capture_installer import install_classifier_agent

    config = tmp_path / "openclaw.json"
    runner = AgentRunner(config)
    workspace = tmp_path / "ws"
    for _ in range(2):
        install_classifier_agent(
            agent_id="albert-classifier",
            workspace=workspace,
            model="openai/other-model",
            runner=runner,
            probe=lambda *_: None,
        )
    data = json.loads(config.read_text(encoding="utf-8"))
    matching = [a for a in data["agents"]["list"] if a["id"] == "albert-classifier"]
    assert len(matching) == 1
    assert matching[0]["models"] == {"openai/other-model": {"agentRuntime": {"id": "openclaw"}}}
    assert sum(1 for c in runner.commands if c[1:3] == ["agents", "add"]) == 1


def test_classifier_agent_install_rolls_back_when_the_probe_fails(tmp_path: Path):
    from albert.capture_installer import InstallError, install_classifier_agent

    config = tmp_path / "openclaw.json"
    runner = AgentRunner(config)
    before = config.read_bytes()
    workspace = tmp_path / "ws"

    def failing_probe(*_: object) -> None:
        raise InstallError("gateway answered 404")

    with pytest.raises(InstallError, match="gateway answered 404"):
        install_classifier_agent(
            agent_id="albert-classifier",
            workspace=workspace,
            model=None,
            runner=runner,
            probe=failing_probe,
        )
    assert config.read_bytes() == before
    assert runner.deleted == ["albert-classifier"]
    assert not workspace.exists()


def test_classifier_agent_install_refuses_a_workspace_it_does_not_own(tmp_path: Path):
    from albert.capture_installer import InstallError, install_classifier_agent

    config = tmp_path / "openclaw.json"
    runner = AgentRunner(config)
    workspace = tmp_path / "someone-elses"
    workspace.mkdir()
    (workspace / "SOUL.md").write_text("mine", encoding="utf-8")
    with pytest.raises(InstallError, match="not owned by Albert"):
        install_classifier_agent(
            agent_id="albert-classifier",
            workspace=workspace,
            model=None,
            runner=runner,
            probe=lambda *_: None,
        )
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == "mine"
    assert runner.commands == [] or all(c[1:3] != ["agents", "add"] for c in runner.commands)


def test_classifier_agent_remove_deletes_only_an_owned_agent(tmp_path: Path):
    from albert.capture_installer import (
        InstallError,
        install_classifier_agent,
        remove_classifier_agent,
    )

    config = tmp_path / "openclaw.json"
    runner = AgentRunner(config)
    workspace = tmp_path / "ws"
    install_classifier_agent(
        agent_id="albert-classifier",
        workspace=workspace,
        model=None,
        runner=runner,
        probe=lambda *_: None,
    )
    remove_classifier_agent(agent_id="albert-classifier", workspace=workspace, runner=runner)
    assert runner.deleted == ["albert-classifier"]
    assert not workspace.exists()

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    with pytest.raises(InstallError, match="not owned by Albert"):
        remove_classifier_agent(agent_id="main", workspace=foreign, runner=runner)
    assert runner.deleted == ["albert-classifier"]


def test_classifier_agent_accepts_openclaw_releases_older_than_the_capture_plugin(tmp_path: Path):
    from albert.capture_installer import InstallError, install_classifier_agent

    config = tmp_path / "openclaw.json"
    install_classifier_agent(
        workspace=tmp_path / "ws",
        runner=AgentRunner(config, version="2026.6.35"),
        probe=lambda *_: None,
    )
    assert any(
        a["id"] == "albert-classifier" for a in json.loads(config.read_text())["agents"]["list"]
    )

    with pytest.raises(InstallError, match=r"unsupported; require >=2026\.6\.35"):
        install_classifier_agent(
            workspace=tmp_path / "ws2",
            runner=AgentRunner(tmp_path / "old.json", version="2026.5.9"),
            probe=lambda *_: None,
        )


def test_config_file_path_ignores_the_cli_health_banner(tmp_path: Path):
    from albert.capture_installer import _config_file_path

    config = tmp_path / "openclaw.json"

    def chatty(command: list[str]) -> subprocess.CompletedProcess[str]:
        banner = "│ Left legacy config health state in place │\n╰──────╯\n"
        return subprocess.CompletedProcess(command, 0, f"{banner}{config}\n", "")

    assert _config_file_path(chatty) == config
