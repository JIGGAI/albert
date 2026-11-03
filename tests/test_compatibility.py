from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from typer.testing import CliRunner

import albert
from albert import capture, capture_sdk, schemas
from albert.admin import app
from albert.compatibility import (
    MIGRATIONS_DIR_ENV,
    MigrationsUnavailable,
    compatibility_manifest,
    resolve_migrations_dir,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
SCHEMA = json.loads((REPO_ROOT / "contracts" / "compatibility-v1.schema.json").read_text())


def _alembic_script(monkeypatch: pytest.MonkeyPatch) -> ScriptDirectory:
    monkeypatch.chdir(REPO_ROOT)  # alembic.ini's script_location is cwd-relative
    return ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))


def _validate(document: dict, definition: str) -> None:
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema ships transitively via mcp
        required = SCHEMA["$defs"][definition]["required"]
        assert set(required) <= set(document)
        return
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    subschema = {"$defs": SCHEMA["$defs"], "$ref": f"#/$defs/{definition}"}
    jsonschema.Draft202012Validator(subschema).validate(document)
    jsonschema.Draft202012Validator(SCHEMA).validate(document)


def _stripped_env(**extra: str) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(SRC_DIR)}
    env.update(extra)
    return env


def test_manifest_heads_match_alembic(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _alembic_script(monkeypatch)
    migrations = compatibility_manifest()["migrations"]

    assert migrations["heads"] == sorted(script.get_heads())
    assert migrations["heads"]


def test_known_revisions_cover_every_file_in_base_to_head_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _alembic_script(monkeypatch)
    known = compatibility_manifest()["migrations"]["known_revisions"]

    file_ids = {
        rev.revision
        for rev in script.walk_revisions()
        if Path(rev.path).parent == REPO_ROOT / "migrations" / "versions"
    }
    assert len(file_ids) == len(list((REPO_ROOT / "migrations" / "versions").glob("*.py")))
    assert file_ids <= set(known)
    assert len(known) == len(set(known))
    assert known[-1] in script.get_heads()
    assert script.get_revision(known[0]).down_revision is None
    for position, revision in enumerate(known):
        for parent in script.get_revision(revision)._all_down_revisions:
            assert known.index(parent) < position


def test_contract_constants_are_consistent() -> None:
    contracts = compatibility_manifest()["contracts"]

    assert contracts["rest_api"] == ["v1"]
    assert schemas.CAPTURE_CONTRACT_VERSION in contracts["capture"]
    assert capture.CAPTURE_CONTRACT_VERSION in contracts["capture"]
    assert capture_sdk.CONTRACT_VERSION in contracts["capture"]
    assert set(contracts["capture"]) == {capture.CAPTURE_CONTRACT_VERSION}
    assert contracts["export_format"] == [schemas.EXPORT_FORMAT_VERSION]
    # /v1/export responds with ExportBundle; its format field defaults to the constant.
    assert (
        schemas.ExportBundle.model_fields["format_version"].default == schemas.EXPORT_FORMAT_VERSION
    )


def test_cli_prints_compact_schema_valid_json() -> None:
    result = CliRunner().invoke(app, ["compat", "--json"])

    assert result.exit_code == 0, result.output
    assert result.stdout.endswith("\n")
    assert result.stdout.count("\n") == 1
    assert ": " not in result.stdout and ", " not in result.stdout
    manifest = json.loads(result.stdout)
    _validate(manifest, "manifest")
    assert manifest["package"] == "albert-memory"
    assert manifest["package_version"] == albert.__version__
    assert "admin" not in manifest["contracts"]
    assert CliRunner().invoke(app, ["compat"]).stdout == result.stdout


def test_compat_runs_with_an_empty_environment(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "albert.admin", "compat", "--json"],
        cwd=tmp_path,
        env=_stripped_env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads(completed.stdout)
    _validate(manifest, "manifest")
    assert manifest["migrations"]["heads"]
    assert list(tmp_path.iterdir()) == []  # no SQLite file or other side effects


def test_compatibility_module_does_not_load_database_or_settings() -> None:
    probe = (
        "import sys, albert.compatibility as c; c.compatibility_manifest();"
        "print(sorted(m for m in ('albert.db', 'albert.config') if m in sys.modules))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        env=_stripped_env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert completed.stdout.strip() == "[]"


def test_missing_migrations_dir_exits_non_zero_with_json_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing"
    monkeypatch.setenv(MIGRATIONS_DIR_ENV, str(missing))
    with pytest.raises(MigrationsUnavailable):
        resolve_migrations_dir()

    completed = subprocess.run(
        [sys.executable, "-m", "albert.admin", "compat", "--json"],
        cwd=tmp_path,
        env=_stripped_env(**{MIGRATIONS_DIR_ENV: str(missing)}),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode != 0
    assert completed.stdout == ""
    error = json.loads(completed.stderr.strip().splitlines()[-1])
    _validate(error, "error")
    assert error["error"]["code"] == "migrations_unavailable"
    assert str(missing) in error["error"]["message"]


def test_empty_migrations_dir_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "versions").mkdir()
    monkeypatch.setenv(MIGRATIONS_DIR_ENV, str(tmp_path))

    result = CliRunner().invoke(app, ["compat"])

    assert result.exit_code != 0
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "migrations_unavailable"


def test_alembic_ini_is_resolved_independently_of_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(MIGRATIONS_DIR_ENV, raising=False)
    monkeypatch.chdir(tmp_path)

    assert resolve_migrations_dir() == REPO_ROOT / "migrations"
