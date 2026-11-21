from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _alembic(project_root: Path, database: Path, *arguments: str) -> None:
    environment = {
        **os.environ,
        "ALBERT_DATABASE_URL": f"sqlite:///{database}",
        "ALBERT_API_KEY_PEPPER": "migration-test-pepper-longer-than-thirty-two-characters",
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=project_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def test_oauth_migration_round_trip(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    database = tmp_path / "oauth-migration.sqlite3"
    _alembic(project_root, database, "upgrade", "0020")
    _alembic(project_root, database, "check")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"oauth_clients", "oauth_authorization_codes"}.issubset(tables)
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list('oauth_authorization_codes')")
        }
        assert {
            "ix_oauth_authorization_codes_client_id",
            "ix_oauth_authorization_codes_expires_at",
        }.issubset(indexes)
    _alembic(project_root, database, "downgrade", "0019")
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'oauth_clients'"
            ).fetchone()
            is None
        )
