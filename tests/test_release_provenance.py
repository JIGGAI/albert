import json
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "verify-image-provenance.sh"
REVISION = "a" * 40
TREE = "b" * 40


def verify(tmp_path: Path, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    provenance = tmp_path / "image-provenance.json"
    provenance.write_text(json.dumps(payload))
    return subprocess.run(
        ["sh", str(SCRIPT), str(provenance), REVISION, TREE],
        capture_output=True,
        check=False,
        text=True,
    )


def payload(mode: str = "promoted") -> dict[str, object]:
    return {
        "schema": 1,
        "mode": mode,
        "image": "ghcr.io/jiggai/albert",
        "digest": f"sha256:{'c' * 64}",
        "final_revision": REVISION,
        "tree": TREE,
        "candidate": f"ghcr.io/jiggai/albert:candidate-tree-{TREE}",
        "ci_run": "https://github.com/JIGGAI/albert/actions/runs/1234",
    }


def test_accepts_promoted_merge_tree_equivalence(tmp_path: Path) -> None:
    assert verify(tmp_path, payload()).returncode == 0


def test_accepts_automatic_final_tree_build(tmp_path: Path) -> None:
    candidate = payload("built")
    candidate.pop("candidate")
    assert verify(tmp_path, candidate).returncode == 0


def test_rejects_stale_tree(tmp_path: Path) -> None:
    candidate = payload()
    candidate["tree"] = "d" * 40
    assert verify(tmp_path, candidate).returncode != 0
