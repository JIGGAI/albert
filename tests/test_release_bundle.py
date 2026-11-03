from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "release-bundle.py"
SPEC = importlib.util.spec_from_file_location("release_bundle", SCRIPT)
assert SPEC and SPEC.loader
release_bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_bundle)

REVISION, TREE = "a" * 40, "b" * 40
COMPAT = {
    "compatibility_manifest": "1",
    "package": "albert-memory",
    "package_version": "0.4.0",
    "contracts": {"rest_api": ["v1"], "capture": ["1"], "export_format": ["albert-export-v1"]},
    "migrations": {"heads": ["0014"], "known_revisions": ["0014"]},
}


def fixture() -> dict:
    images = {}
    for index, (name, repository) in enumerate(release_bundle.IMAGE_NAMES.items(), 1):
        digest = "sha256:" + str(index) * 64
        images[name] = {
            "repository": repository,
            "digest": digest,
            "reference": f"{repository}@{digest}",
            "source_revision": REVISION,
            "compatibility": copy.deepcopy(COMPAT),
            "compatibility_sha256": "sha256:" + "3" * 64,
            "sbom": f"oci://{repository}@{digest}#sbom",
            "provenance": f"oci://{repository}@{digest}#provenance",
            "artifact_test": "passed",
        }
    return {
        "schema": "albert-release-bundle/v1",
        "source_revision": REVISION,
        "source_tree": TREE,
        "images": images,
        "runtime_contract": {
            "semantic_model": "BAAI/bge-small-en-v1.5",
            "embedding_dimensions": 384,
        },
        "ci_run": "https://github.com/JIGGAI/albert/actions/runs/1",
    }


def test_valid_dual_image_bundle() -> None:
    release_bundle.verify_value(fixture(), REVISION, TREE)


@pytest.mark.parametrize("missing", ["standard", "semantic"])
def test_missing_artifact_fails_closed(missing: str) -> None:
    value = fixture()
    del value["images"][missing]
    with pytest.raises(release_bundle.InvalidBundle, match="exactly standard and semantic"):
        release_bundle.verify_value(value)


def test_mutable_reference_fails_closed() -> None:
    value = fixture()
    value["images"]["standard"]["reference"] = "ghcr.io/jiggai/albert:latest"
    with pytest.raises(release_bundle.InvalidBundle, match="not immutable"):
        release_bundle.verify_value(value)


def test_cross_revision_artifact_fails_closed() -> None:
    value = fixture()
    value["images"]["semantic"]["source_revision"] = "c" * 40
    with pytest.raises(release_bundle.InvalidBundle, match="different revision"):
        release_bundle.verify_value(value)


def test_incompatible_schema_fails_closed() -> None:
    value = fixture()
    value["images"]["semantic"]["compatibility"]["migrations"]["heads"] = ["0015"]
    with pytest.raises(release_bundle.InvalidBundle, match="not schema-compatible"):
        release_bundle.verify_value(value)


def test_digest_mismatched_evidence_fails_closed() -> None:
    value = fixture()
    value["images"]["semantic"]["sbom"] = value["images"]["standard"]["sbom"]
    with pytest.raises(release_bundle.InvalidBundle, match="does not match"):
        release_bundle.verify_value(value)
