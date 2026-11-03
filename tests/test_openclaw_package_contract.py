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
            "albert/capture_installer.py", "albert/openclaw_adapter.py",
            "albert/openclaw_adapter_trust.json", "albert/openclaw_package.json",
            "albert/openclaw_plugin/index.js",
            "albert/openclaw_plugin/openclaw.plugin.json",
            "albert/openclaw_plugin/package.json",
        }
        for name in required:
            archive.writestr(name, descriptor if name.endswith("openclaw_package.json") else b"x")
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


def test_docs_do_not_regress_to_unsafe_openclaw_examples() -> None:
    text = (ROOT / "docs/integrations/openclaw.md").read_text(encoding="utf-8")
    assert "albert-capture connect" in text
    assert "Advanced: manual MCP and REST clients" in text
    assert "SecretRef" not in text
    assert "ALBERT_MCP_API_KEY=" not in text
    assert "http://127.0.0.1" not in text
