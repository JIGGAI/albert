import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts/openclaw-package-contract.py"


def run(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_source_package_contract_is_consistent() -> None:
    assert run("source", str(ROOT)).returncode == 0
    assert run("source", str(ROOT), "--require-manifest-trust").returncode != 0


def test_release_injects_and_requires_manifest_public_key(tmp_path: Path) -> None:
    source = tmp_path / "src" / "albert"
    source.mkdir(parents=True)
    trust_path = source / "openclaw_adapter_trust.json"
    trust_path.write_text('{"manifest_keys": {}}\n', encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "ALBERT_ADAPTER_MANIFEST_KEY_ID": "albert-cloud-production-1",
            "ALBERT_ADAPTER_MANIFEST_PUBLIC_KEY": "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE",
        }
    )

    assert run("inject-trust", str(tmp_path), env=env).returncode == 0
    assert json.loads(trust_path.read_text()) == {
        "manifest_keys": {
            "albert-cloud-production-1": "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE"
        }
    }
    env["ALBERT_ADAPTER_MANIFEST_PUBLIC_KEY"] = "invalid"
    assert run("inject-trust", str(tmp_path), env=env).returncode != 0


def test_wheel_provenance_rejects_missing_component_and_drift(tmp_path: Path) -> None:
    wheel = tmp_path / "albert_memory-0.4.0-py3-none-any.whl"
    descriptor = (ROOT / "src/albert/openclaw_package.json").read_bytes()
    with zipfile.ZipFile(wheel, "w") as archive:
        required = {
            "albert/capture_installer.py",
            "albert/openclaw_adapter.py",
            "albert/host_targets.py",
            "albert/host_targets/openclaw.json",
            "albert/openclaw_adapter_trust.json",
            "albert/openclaw_package.json",
            "albert/openclaw_plugin/index.js",
            "albert/openclaw_plugin/openclaw.plugin.json",
            "albert/openclaw_plugin/package.json",
        }
        for name in required:
            if name.endswith("openclaw_package.json"):
                content = descriptor
            elif name.endswith("host_targets/openclaw.json"):
                content = (ROOT / "src" / name).read_bytes()
            elif name.endswith("openclaw_adapter_trust.json"):
                content = b'{"manifest_keys": {}}\n'
            else:
                content = b"x"
            archive.writestr(name, content)
        archive.writestr("albert_memory-0.4.0.dist-info/METADATA", "Version: 0.4.0\n")
    provenance = tmp_path / "provenance.json"
    revision = "a" * 40
    emitted = run(
        "emit", str(wheel), "--source-revision", revision, "--output", str(provenance)
    )
    assert emitted.returncode == 0
    required = run(
        "emit",
        str(wheel),
        "--source-revision",
        revision,
        "--output",
        str(provenance),
        "--require-manifest-trust",
    )
    assert required.returncode != 0
    assert run("verify", str(wheel), str(provenance)).returncode == 0

    trusted_wheel = tmp_path / "trusted-albert-memory.whl"
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(trusted_wheel, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            if name.endswith("openclaw_adapter_trust.json"):
                content = (
                    b'{"manifest_keys":{"albert-cloud-production-1":'
                    b'"AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE"}}\n'
                )
            target.writestr(name, content)
    assert (
        run(
            "emit",
            str(trusted_wheel),
            "--source-revision",
            revision,
            "--output",
            str(provenance),
            "--require-manifest-trust",
        ).returncode
        == 0
    )

    value = json.loads(provenance.read_text())
    value["artifact_sha256"] = "0" * 64
    provenance.write_text(json.dumps(value))
    assert run("verify", str(wheel), str(provenance)).returncode != 0


def test_docs_do_not_regress_to_unsafe_openclaw_examples() -> None:
    text = (ROOT / "docs/integrations/openclaw.md").read_text(encoding="utf-8")
    assert "albert-capture connect" in text
    assert "Advanced: manual MCP and REST clients" in text
    assert "SecretRef" not in text
    assert "ALBERT_MCP_API_KEY=" not in text
    assert "http://127.0.0.1" not in text
