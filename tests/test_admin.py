import json
import re
from pathlib import Path
from uuid import UUID

import pytest
from click.utils import strip_ansi
from jsonschema import Draft202012Validator
from sqlalchemy import select
from typer.testing import CliRunner

import albert
from albert.admin import ADMIN_CONTRACT_VERSION, app
from albert.db import SessionLocal
from albert.models import APIKey, Organization, Principal


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


SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts" / "admin-v1.schema.json").read_text()
)
UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _validate(document: object, definition: str) -> None:
    schema = {"$ref": f"#/$defs/{definition}", "$defs": SCHEMA["$defs"]}
    Draft202012Validator(schema).validate(document)
    # The top-level oneOf must recognize every emitted document unambiguously.
    Draft202012Validator(SCHEMA).validate(document)


def _invoke_json(*args: str) -> dict:
    result = CliRunner().invoke(app, [*args, "--json"])
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert result.stdout.endswith("\n") and result.stdout.count("\n") == 1
    return json.loads(result.stdout)


def _bootstrap(organization: str) -> None:
    result = CliRunner().invoke(
        app, ["bootstrap", "--organization", organization, "--workspace", "Default"]
    )
    assert result.exit_code == 0, result.output


def test_schema_is_valid_draft_2020_12_with_per_command_defs() -> None:
    Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["$id"] == "https://schemas.jiggai.com/albert/admin-v1.schema.json"
    for name in (
        "create_key",
        "create_principal",
        "create_workspace",
        "list_principals",
        "list_keys",
        "revoke_key",
        "contract",
        "error",
        "principal",
        "key",
        "workspace",
    ):
        assert name in SCHEMA["$defs"]


def test_text_output_format_is_unchanged() -> None:
    runner = CliRunner()
    organization = "CLI Text Golden"
    _bootstrap(organization)

    workspace = runner.invoke(
        app, ["create-workspace", "--organization", organization, "--name", "Agents"]
    )
    assert workspace.exit_code == 0
    assert re.fullmatch(rf"{UUID_PATTERN}\n", workspace.output)
    workspace_id = workspace.output.strip()
    again = runner.invoke(
        app, ["create-workspace", "--organization", organization, "--name", "Agents"]
    )
    assert again.output == workspace.output

    scoped = runner.invoke(
        app,
        [
            "create-principal",
            "--organization",
            organization,
            "--workspace",
            "Agents",
            "--name",
            "Golden Agent",
        ],
    )
    assert re.fullmatch(rf"{UUID_PATTERN}\n", scoped.output)
    scoped_id = scoped.output.strip()
    wide = runner.invoke(
        app,
        [
            "create-principal",
            "--organization",
            organization,
            "--name",
            "Golden Service",
            "--principal-type",
            "service",
        ],
    )
    wide_id = wide.output.strip()

    with SessionLocal() as session:
        admin_id = session.scalar(
            select(Principal.id)
            .join(Organization, Organization.id == Principal.organization_id)
            .where(Organization.name == organization, Principal.name == "Administrator")
        )
        default_id = session.scalar(select(Principal.workspace_id).where(Principal.id == admin_id))
    listing = runner.invoke(app, ["list-principals", "--organization", organization])
    assert listing.exit_code == 0
    assert listing.output == (
        f"{admin_id}\tAdministrator\thuman\tworkspace={default_id}\tactive=True\n"
        f"{scoped_id}\tGolden Agent\tagent\tworkspace={workspace_id}\tactive=True\n"
        f"{wide_id}\tGolden Service\tservice\tworkspace=*\tactive=True\n"
    )

    created = runner.invoke(
        app, ["create-key", "--principal-id", scoped_id, "--name", "golden key"]
    )
    assert re.fullmatch(r"alb_\S+\n", created.output)
    keys = runner.invoke(app, ["list-keys", "--principal-id", scoped_id])
    with SessionLocal() as session:
        key = session.scalar(select(APIKey).where(APIKey.principal_id == UUID(scoped_id)))
    assert keys.output == (f"{key.id}\tgolden key\tprefix={key.prefix}\tactive=True\texpires=-\n")
    assert created.output.startswith(f"alb_{key.prefix}_")

    revoked = runner.invoke(app, ["revoke-key", "--key-id", str(key.id)])
    assert revoked.output == "API key revoked\n"
    keys = runner.invoke(app, ["list-keys", "--principal-id", scoped_id])
    assert keys.output == (f"{key.id}\tgolden key\tprefix={key.prefix}\tactive=False\texpires=-\n")


def test_text_mode_errors_are_unchanged() -> None:
    runner = CliRunner()
    missing = runner.invoke(app, ["list-principals", "--organization", "No Such Org"])
    assert missing.exit_code == 2
    assert "Organization not found" in missing.output
    malformed = runner.invoke(app, ["list-keys", "--principal-id", "not-a-uuid"])
    assert malformed.exit_code == 1
    assert isinstance(malformed.exception, ValueError)


def test_json_lifecycle_covers_every_command() -> None:
    organization = "CLI JSON Lifecycle"
    _bootstrap(organization)

    workspace = _invoke_json("create-workspace", "--organization", organization, "--name", "Ops")
    _validate(workspace, "create_workspace")
    assert workspace["created"] is True
    assert workspace["workspace"]["name"] == "Ops"
    existing = _invoke_json("create-workspace", "--organization", organization, "--name", "Ops")
    _validate(existing, "create_workspace")
    assert existing["created"] is False
    assert existing["workspace"] == workspace["workspace"]

    scoped = _invoke_json(
        "create-principal",
        "--organization",
        organization,
        "--workspace",
        "Ops",
        "--name",
        "Ops Agent",
    )
    _validate(scoped, "create_principal")
    assert scoped["principal"]["workspace_id"] == workspace["workspace"]["id"]
    assert scoped["principal"]["principal_type"] == "agent"
    assert scoped["principal"]["active"] is True
    wide = _invoke_json(
        "create-principal",
        "--organization",
        organization,
        "--name",
        "Org Human",
        "--principal-type",
        "human",
    )
    _validate(wide, "create_principal")
    assert wide["principal"]["workspace_id"] is None

    listing = _invoke_json("list-principals", "--organization", organization)
    _validate(listing, "list_principals")
    assert listing["organization"]["name"] == organization
    assert [item["name"] for item in listing["principals"]] == [
        "Administrator",
        "Ops Agent",
        "Org Human",
    ]
    assert scoped["principal"] in listing["principals"]
    text_ids = [
        line.split("\t", 1)[0]
        for line in CliRunner()
        .invoke(app, ["list-principals", "--organization", organization])
        .output.splitlines()
    ]
    assert [item["id"] for item in listing["principals"]] == text_ids

    principal_id = scoped["principal"]["id"]
    created = _invoke_json(
        "create-key",
        "--principal-id",
        principal_id,
        "--name",
        "ops key",
        "--capabilities",
        "memory.read, graph.query",
    )
    _validate(created, "create_key")
    assert created["credential"].startswith("alb_")
    assert created["key"]["principal_id"] == principal_id
    assert created["key"]["capabilities"] == ["memory.read", "graph.query"]
    assert created["key"]["active"] is True
    assert created["key"]["expires_at"] is None
    assert created["key"]["created_at"].endswith("+00:00")

    keys = _invoke_json("list-keys", "--principal-id", principal_id)
    _validate(keys, "list_keys")
    assert keys["principal_id"] == principal_id
    assert keys["keys"] == [created["key"]]

    revoked = _invoke_json("revoke-key", "--key-id", created["key"]["id"])
    _validate(revoked, "revoke_key")
    assert revoked == {"admin_contract": "1", "key": {"id": created["key"]["id"], "active": False}}
    after = _invoke_json("list-keys", "--principal-id", principal_id)
    assert after["keys"][0]["active"] is False


def test_list_keys_json_never_exposes_secrets() -> None:
    organization = "CLI JSON Secrets"
    _bootstrap(organization)
    listing = _invoke_json("list-principals", "--organization", organization)
    principal_id = listing["principals"][0]["id"]
    credential = _invoke_json("create-key", "--principal-id", principal_id)["credential"]

    result = CliRunner().invoke(app, ["list-keys", "--principal-id", principal_id, "--json"])
    assert result.exit_code == 0
    assert credential not in result.stdout
    document = json.loads(result.stdout)
    _validate(document, "list_keys")
    assert len(document["keys"]) == 2
    for key in document["keys"]:
        assert set(key) == set(SCHEMA["$defs"]["key"]["required"])
        assert not {"key_hash", "hash", "secret", "credential", "raw"} & set(key)
    with SessionLocal() as session:
        digests = session.scalars(
            select(APIKey.key_hash).where(APIKey.principal_id == UUID(principal_id))
        )
        for digest in digests:
            assert digest.hex() not in result.stdout


def test_contract_command() -> None:
    for args in (["contract"], ["contract", "--json"]):
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 0, result.output
        document = json.loads(result.stdout)
        _validate(document, "contract")
        assert document == {
            "admin_contract": ADMIN_CONTRACT_VERSION,
            "package": "albert-memory",
            "package_version": albert.__version__,
            "json_commands": [
                "chatgpt-login-poll",
                "chatgpt-login-start",
                "chatgpt-logout",
                "chatgpt-status",
                "contract",
                "create-key",
                "create-principal",
                "create-workspace",
                "list-keys",
                "list-principals",
                "revoke-key",
            ],
        }
    assert ADMIN_CONTRACT_VERSION == "1"


def test_every_listed_json_command_accepts_the_flag() -> None:
    document = json.loads(CliRunner().invoke(app, ["contract"]).stdout)
    for command in document["json_commands"]:
        result = CliRunner().invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "--json" in strip_ansi(result.output)


@pytest.fixture(scope="module")
def error_organization() -> None:
    _bootstrap("CLI JSON Errors")


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (["list-principals", "--organization", "Missing Org"], "organization_not_found"),
        (
            ["create-workspace", "--organization", "Missing Org", "--name", "X"],
            "organization_not_found",
        ),
        (
            [
                "create-principal",
                "--organization",
                "CLI JSON Errors",
                "--name",
                "X",
                "--workspace",
                "Nope",
            ],
            "workspace_not_found",
        ),
        (
            [
                "create-principal",
                "--organization",
                "CLI JSON Errors",
                "--name",
                "X",
                "--principal-type",
                "robot",
            ],
            "invalid_principal_type",
        ),
        (
            ["list-keys", "--principal-id", "00000000-0000-0000-0000-000000000000"],
            "principal_not_found",
        ),
        (
            ["create-key", "--principal-id", "00000000-0000-0000-0000-000000000000"],
            "principal_not_found",
        ),
        (["revoke-key", "--key-id", "00000000-0000-0000-0000-000000000000"], "api_key_not_found"),
        (
            [
                "create-key",
                "--principal-id",
                "00000000-0000-0000-0000-000000000000",
                "--capabilities",
                "memory.read,bogus",
            ],
            "unknown_capabilities",
        ),
        (["list-keys", "--principal-id", "not-a-uuid"], "invalid_id"),
        (["create-key", "--principal-id", "not-a-uuid"], "invalid_id"),
        (["revoke-key", "--key-id", "not-a-uuid"], "invalid_id"),
    ],
)
def test_json_errors_go_to_stderr_with_exit_code_2(
    error_organization: None, args: list[str], code: str
) -> None:
    result = CliRunner().invoke(app, [*args, "--json"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.endswith("\n") and result.stderr.count("\n") == 1
    document = json.loads(result.stderr)
    _validate(document, "error")
    assert document["admin_contract"] == "1"
    assert document["error"]["code"] == code
    assert document["error"]["message"]
