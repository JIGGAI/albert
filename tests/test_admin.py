from typer.testing import CliRunner

from albert.admin import app


def test_validate_runtime_probes_configured_embedder() -> None:
    result = CliRunner().invoke(app, ["validate-runtime", "--probe-embedding"])
    assert result.exit_code == 0, result.output
    assert "embedding=hashing:" in result.output
    assert "embedding probe succeeded (64 dimensions)" in result.output
