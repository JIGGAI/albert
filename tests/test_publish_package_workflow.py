from pathlib import Path

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "publish-package.yml"


def test_pypi_release_is_manual_and_prepares_a_reviewed_version_bump() -> None:
    text = WORKFLOW.read_text()

    assert "workflow_dispatch:" in text
    assert "release:\n    types: [published]" not in text
    assert "RELEASE_VERSION: ${{ inputs.version }}" in text
    assert 'test "$GITHUB_REF" = "refs/heads/main"' in text
    assert "python scripts/bump-package-version.py" in text
    assert "pull-requests: write" in text
    assert "gh pr create" in text
    assert "gh workflow run ci.yml" in text
    assert "if: needs.prepare.outputs.release_ready == 'true'" in text
    assert "--workflow ci.yml --branch main" in text
    assert "openclaw-package-contract.py source" in text


def test_pypi_release_uses_trusted_publishing_without_docker_rollout() -> None:
    text = WORKFLOW.read_text()

    assert "id-token: write" in text
    assert "pypa/gh-action-pypi-publish@release/v1" in text
    assert "actions/attest-build-provenance@v2" in text
    assert "docker" not in text.lower()
    assert "ghcr.io" not in text.lower()
