from uuid import UUID

from typer.testing import CliRunner

from albert.admin import app


def test_validate_runtime_probes_configured_embedder() -> None:
    result = CliRunner().invoke(app, ["validate-runtime", "--probe-embedding"])
    assert result.exit_code == 0, result.output
    assert "embedding=hashing:" in result.output
    assert "embedding probe succeeded (64 dimensions)" in result.output


def test_principal_workspace_and_key_lifecycle_commands() -> None:
    runner = CliRunner()
    organization = "CLI Lifecycle"
    bootstrap = runner.invoke(
        app,
        [
            "bootstrap",
            "--organization",
            organization,
            "--workspace",
            "Default",
        ],
    )
    assert bootstrap.exit_code == 0, bootstrap.output

    workspace = runner.invoke(
        app,
        [
            "create-workspace",
            "--organization",
            organization,
            "--name",
            "Agents",
        ],
    )
    assert workspace.exit_code == 0, workspace.output
    UUID(workspace.output.strip())

    principal = runner.invoke(
        app,
        [
            "create-principal",
            "--organization",
            organization,
            "--workspace",
            "Agents",
            "--name",
            "Release Agent",
        ],
    )
    assert principal.exit_code == 0, principal.output
    principal_id = UUID(principal.output.strip())

    listing = runner.invoke(app, ["list-principals", "--organization", organization])
    assert listing.exit_code == 0
    assert "Release Agent" in listing.output

    created_key = runner.invoke(app, ["create-key", "--principal-id", str(principal_id)])
    assert created_key.exit_code == 0, created_key.output
    assert created_key.output.startswith("alb_")
    keys = runner.invoke(app, ["list-keys", "--principal-id", str(principal_id)])
    assert keys.exit_code == 0, keys.output
    key_id = keys.output.split("\t", 1)[0]
    revoked = runner.invoke(app, ["revoke-key", "--key-id", key_id])
    assert revoked.exit_code == 0, revoked.output
    assert "revoked" in revoked.output
