from __future__ import annotations

import json
from pathlib import Path

from albert.release_gate import GOLDEN_FLOW, HOSTS, load_manifest, receipt, validate_manifest


def test_release_manifest_closes_every_declared_gate() -> None:
    manifest, _ = load_manifest()
    assert validate_manifest(manifest) == []
    assert set(manifest["hosts"]) == HOSTS
    assert all(set(host["golden_flow"]) == GOLDEN_FLOW for host in manifest["hosts"].values())
    assert manifest["release"]["general_availability"] is False
    assert manifest["release"]["channel"] == "opt-in-beta"


def test_release_evidence_and_documentation_are_present() -> None:
    root = Path(__file__).parents[1]
    manifest, _ = load_manifest()
    referenced = list(manifest["evidence"])
    for host in manifest["hosts"].values():
        referenced.extend((host["fixture"], host["documentation"]))
    assert all((root / item).is_file() for item in referenced)
    runbook = (root / "docs/AUTOMATIC_MEMORY_RELEASE.md").read_text(encoding="utf-8")
    for heading in (
        "Supported versions",
        "Golden flow",
        "SLOs and recovery bounds",
        "Security and data handling",
        "Migration and rollback",
        "Deletion",
        "Known limitations",
        "Opt-in beta",
    ):
        assert f"## {heading}" in runbook


def test_release_receipt_is_machine_readable_and_content_addressed() -> None:
    result = receipt()
    assert result["status"] == "PASS"
    assert result["hosts"] == sorted(HOSTS)
    assert len(result["manifest_sha256"]) == 64
    assert json.loads(json.dumps(result)) == result
