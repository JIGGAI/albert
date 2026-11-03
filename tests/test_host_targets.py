from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from albert.host_targets import (
    HostTargetError,
    assert_owned_install_root,
    load_host_target,
    negotiate_host_target,
    validate_host_target,
)


def descriptor() -> dict[str, object]:
    path = Path(__file__).parents[1] / "src/albert/host_targets/openclaw.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_openclaw_exact_release_passes_complete_lifecycle_conformance():
    target = negotiate_host_target(
        "openclaw",
        (2026, 9, 6),
        {
            "capture",
            "recall",
            "direct_brain",
            "upgrade",
            "rotate",
            "revoke",
            "uninstall",
            "rollback",
        },
    )

    assert target.descriptor_version == "1.0.0"
    assert target.hooks == {
        "fidelity": "full",
        "present": [
            "session_start",
            "user_prompt",
            "pre_compaction",
            "run_complete",
            "session_end",
        ],
        "absent": [],
    }
    assert target.credentials["storage"] == "owned_file_0600"
    assert all(target.lifecycle.values())


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda value: value.update(schema_version=2), "schema or version"),
        (lambda value: value.update(enabled=False), "disabled"),
        (lambda value: value.update(extra=True), "unknown or missing"),
        (lambda value: value["host"].update(supported=">=2027.0.0"), "outside"),
        (lambda value: value["install"].update(root="~/../foreign"), "unsafe"),
        (
            lambda value: value["hooks"].update(fidelity="full", absent=["session_end"]),
            "contradictory",
        ),
        (lambda value: value["credentials"].update(storage="ordinary_env"), "unsafe"),
        (lambda value: value["lifecycle"].update(rollback=False), "incomplete"),
    ],
)
def test_malicious_or_incomplete_descriptors_fail_closed(mutation, match):
    payload = deepcopy(descriptor())
    mutation(payload)

    with pytest.raises(HostTargetError, match=match):
        validate_host_target(payload)


def test_unknown_target_version_and_capability_fail_without_loading_a_fallback():
    with pytest.raises(HostTargetError, match="unknown or disabled"):
        load_host_target("codex")
    with pytest.raises(HostTargetError, match="version is unsupported"):
        negotiate_host_target("openclaw", (2027, 0, 0), {"capture"})
    with pytest.raises(HostTargetError, match="lacks required"):
        negotiate_host_target("openclaw", (2026, 9, 6), {"fabricated_hook"})


def test_reduced_fidelity_requires_missing_hooks_to_be_absent_and_declared():
    payload = descriptor()
    payload["hooks"]["fidelity"] = "reduced"
    payload["hooks"]["present"].remove("session_end")
    payload["hooks"]["absent"] = ["session_end"]

    target = validate_host_target(payload)

    assert target.hooks["present"][-1] == "run_complete"
    assert target.hooks["absent"] == ["session_end"]


def test_install_ownership_rejects_symlinks_foreign_markers_and_widened_modes(tmp_path: Path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    symlink = tmp_path / "install-link"
    symlink.symlink_to(foreign, target_is_directory=True)
    with pytest.raises(HostTargetError, match="symlink"):
        assert_owned_install_root(symlink, expected_marker="albert-memory")

    root = tmp_path / "albert"
    root.mkdir()
    marker = root / "install.json"
    marker.write_text('{"owner":"someone-else"}', encoding="utf-8")
    with pytest.raises(HostTargetError, match="foreign ownership"):
        assert_owned_install_root(root, expected_marker="albert-memory")

    marker.write_text('{"owner":"albert-memory"}', encoding="utf-8")
    os.chmod(marker, 0o644)
    with pytest.raises(HostTargetError, match="mode is unsafe"):
        assert_owned_install_root(root, expected_marker="albert-memory")
