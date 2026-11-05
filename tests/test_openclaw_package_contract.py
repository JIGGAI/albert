import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts/openclaw-package-contract.py"


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments], capture_output=True, text=True, check=False
    )


def test_source_package_contract_is_consistent() -> None:
    assert run("source", str(ROOT)).returncode == 0


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
            elif name.endswith(("host_targets/openclaw.json", "openclaw_adapter_trust.json")):
                content = (ROOT / "src" / name).read_bytes()
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
    assert run("verify", str(wheel), str(provenance)).returncode == 0
    value = json.loads(provenance.read_text())
    value["artifact_sha256"] = "0" * 64
    provenance.write_text(json.dumps(value))
    assert run("verify", str(wheel), str(provenance)).returncode != 0


def test_source_contract_rejects_empty_malformed_and_drifted_trust(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    (package_root / "src/albert/openclaw_plugin").mkdir(parents=True)
    (package_root / "src/albert/host_targets").mkdir(parents=True)
    (package_root / "pyproject.toml").write_text(
        "[project]\nversion = \"0.4.0\"\n[project.scripts]\n"
        "albert-capture = \"albert.capture_installer:app\"\n"
        "albert-openclaw-adapter = \"albert.openclaw_adapter:main\"\n"
    )
    for relative in (
        "src/albert/openclaw_package.json",
        "src/albert/openclaw_adapter_trust.json",
        "src/albert/openclaw_plugin/package.json",
        "src/albert/host_targets/openclaw.json",
    ):
        target = package_root / relative
        target.write_bytes((ROOT / relative).read_bytes())

    trust_path = package_root / "src/albert/openclaw_adapter_trust.json"
    trust_path.write_text('{"manifest_keys": {}}')
    assert run("source", str(package_root)).returncode != 0

    trust_path.write_text(
        json.dumps({"manifest_keys": {"bad key": "A" * 43}}), encoding="utf-8"
    )
    assert run("source", str(package_root)).returncode != 0

    trust_path.write_bytes((ROOT / "src/albert/openclaw_adapter_trust.json").read_bytes())
    descriptor_path = package_root / "src/albert/openclaw_package.json"
    descriptor = json.loads(descriptor_path.read_text())
    descriptor["manifest_key_ids"] = ["different-production-key"]
    descriptor_path.write_text(json.dumps(descriptor))
    assert run("source", str(package_root)).returncode != 0


def test_docs_do_not_regress_to_unsafe_openclaw_examples() -> None:
    text = (ROOT / "docs/integrations/openclaw.md").read_text(encoding="utf-8")
    assert "albert-capture connect" in text
    assert "Advanced: manual MCP and REST clients" in text
    assert "SecretRef" not in text
    assert "ALBERT_MCP_API_KEY=" not in text
    assert "http://127.0.0.1" not in text
