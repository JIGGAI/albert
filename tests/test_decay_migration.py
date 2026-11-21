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


def test_decay_migration_round_trip(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    database = tmp_path / "decay-migration.sqlite3"
    _alembic(project_root, database, "upgrade", "0019")
    _alembic(project_root, database, "upgrade", "head")
    _alembic(project_root, database, "check")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"decay_policies", "decay_states"}.issubset(tables)
        columns = {row[1] for row in connection.execute("PRAGMA table_info('profile_assertions')")}
        assert {"decay_multiplier", "decay_reason"}.issubset(columns)
    _alembic(project_root, database, "downgrade", "0018")
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'decay_states'"
            ).fetchone()
            is None
        )
