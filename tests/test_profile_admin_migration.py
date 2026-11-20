from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _alembic(project_root: Path, database: Path, *arguments: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "ALBERT_DATABASE_URL": f"sqlite:///{database}",
            "ALBERT_API_KEY_PEPPER": "migration-test-pepper-longer-than-thirty-two-characters",
        },
    )


def test_profile_admin_migration_upgrade_downgrade_and_reapply(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "profile-admin.sqlite3"
    _alembic(root, database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        columns = {row[1] for row in connection.execute("PRAGMA table_info(profile_assertions)")}
    assert {"profile_bucket_policies", "profile_assertion_history"} <= tables
    assert {"assertion_key", "version", "decision_state", "suppressed"} <= columns
    _alembic(root, database, "check")
    _alembic(root, database, "downgrade", "0017")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "profile_bucket_policies" not in tables
    _alembic(root, database, "upgrade", "head")
