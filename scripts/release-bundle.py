#!/usr/bin/env python3
"""Create and verify the immutable dual-image Albert release bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_NAMES = {"standard": "ghcr.io/jiggai/albert", "semantic": "ghcr.io/jiggai/albert-semantic"}


class InvalidBundle(ValueError):
    pass


def load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidBundle(f"cannot read JSON {path}: {exc}") from exc


def file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def image_record(name: str, digest: str, revision: str, compat: Path) -> dict[str, Any]:
    repository = IMAGE_NAMES[name]
    return {
        "repository": repository,
        "digest": digest,
        "reference": f"{repository}@{digest}",
        "source_revision": revision,
        "compatibility": load(compat),
        "compatibility_sha256": file_digest(compat),
        "sbom": f"oci://{repository}@{digest}#sbom",
        "provenance": f"oci://{repository}@{digest}#provenance",
        "artifact_test": "passed",
    }


def verify_value(
    value: Any,
    expected_revision: str | None = None,
    expected_tree: str | None = None,
) -> None:
    if not isinstance(value, dict) or value.get("schema") != "albert-release-bundle/v1":
        raise InvalidBundle("unsupported or missing bundle schema")
    revision, tree = value.get("source_revision"), value.get("source_tree")
    if not isinstance(revision, str) or not SHA.fullmatch(revision):
        raise InvalidBundle("source_revision must be a full Git SHA")
    if not isinstance(tree, str) or not SHA.fullmatch(tree):
        raise InvalidBundle("source_tree must be a full Git tree SHA")
    if expected_revision and revision != expected_revision:
        raise InvalidBundle("bundle source revision does not match the expected revision")
    if expected_tree and tree != expected_tree:
        raise InvalidBundle("bundle source tree does not match the expected tree")
    images = value.get("images")
    if not isinstance(images, dict) or set(images) != set(IMAGE_NAMES):
        raise InvalidBundle("bundle must contain exactly standard and semantic images")
    compatibilities = []
    for name, repository in IMAGE_NAMES.items():
        image = images[name]
        if not isinstance(image, dict):
            raise InvalidBundle(f"missing {name} image")
        digest = image.get("digest")
        if (
            image.get("repository") != repository
            or not isinstance(digest, str)
            or not DIGEST.fullmatch(digest)
        ):
            raise InvalidBundle(f"{name} image identity or digest is invalid")
        if image.get("reference") != f"{repository}@{digest}":
            raise InvalidBundle(f"{name} image reference is not immutable")
        if image.get("source_revision") != revision:
            raise InvalidBundle(f"{name} image was built from a different revision")
        if image.get("artifact_test") != "passed":
            raise InvalidBundle(f"{name} artifact test did not pass")
        for evidence in ("sbom", "provenance"):
            ref = image.get(evidence)
            expected = f"oci://{repository}@{digest}#{evidence}"
            if ref != expected:
                raise InvalidBundle(f"{name} {evidence} does not match its image digest")
        compat = image.get("compatibility")
        if not isinstance(compat, dict) or compat.get("compatibility_manifest") != "1":
            raise InvalidBundle(f"{name} compatibility manifest is invalid")
        if not compat.get("migrations", {}).get("heads"):
            raise InvalidBundle(f"{name} compatibility manifest has no migration head")
        compatibilities.append(compat)
        compat_digest = image.get("compatibility_sha256")
        if not isinstance(compat_digest, str) or not DIGEST.fullmatch(compat_digest):
            raise InvalidBundle(f"{name} compatibility evidence digest is invalid")
    if compatibilities[0] != compatibilities[1]:
        raise InvalidBundle("standard and semantic images are not schema-compatible")
    runtime = value.get("runtime_contract")
    if not isinstance(runtime, dict) or not str(runtime.get("semantic_model", "")).strip():
        raise InvalidBundle("semantic model identity is missing")
    dimensions = runtime.get("embedding_dimensions")
    if not isinstance(dimensions, int) or dimensions <= 0:
        raise InvalidBundle("embedding dimensions must be a positive integer")


def emit(args: argparse.Namespace) -> dict[str, Any]:
    value = {
        "schema": "albert-release-bundle/v1",
        "source_revision": args.revision,
        "source_tree": args.tree,
        "images": {
            "standard": image_record(
                "standard", args.standard_digest, args.revision, args.standard_compat
            ),
            "semantic": image_record(
                "semantic", args.semantic_digest, args.revision, args.semantic_compat
            ),
        },
        "runtime_contract": {
            "semantic_model": args.semantic_model,
            "embedding_dimensions": args.embedding_dimensions,
        },
        "ci_run": args.ci_run,
    }
    verify_value(value, args.revision, args.tree)
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("emit")
    for flag in ("revision", "tree", "standard-digest", "semantic-digest", "ci-run"):
        create.add_argument(f"--{flag}", required=True)
    create.add_argument("--standard-compat", type=Path, required=True)
    create.add_argument("--semantic-compat", type=Path, required=True)
    create.add_argument("--semantic-model", default="BAAI/bge-small-en-v1.5")
    create.add_argument("--embedding-dimensions", type=int, default=384)
    create.add_argument("--output", type=Path, required=True)
    check = sub.add_parser("verify")
    check.add_argument("bundle", type=Path)
    check.add_argument("--revision")
    check.add_argument("--tree")
    args = parser.parse_args()
    try:
        if args.command == "emit":
            content = json.dumps(emit(args), indent=2, sort_keys=True) + "\n"
            args.output.write_text(content, encoding="utf-8")
        else:
            verify_value(load(args.bundle), args.revision, args.tree)
    except InvalidBundle as exc:
        print(f"release bundle rejected: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
