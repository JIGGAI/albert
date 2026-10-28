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


def _capture_tables(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'capture_%'"
            )
        }


def _columns(database: Path, table: str) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def test_capture_migration_upgrade_rollback_and_reapply(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    database = tmp_path / "capture-migration.sqlite3"
    expected = {
        "capture_sessions",
        "capture_events",
        "capture_checkpoints",
        "capture_profiles",
    }

    _alembic(project_root, database, "upgrade", "head")
    assert _capture_tables(database) == expected
    profile_columns = _columns(database, "capture_profiles")
    event_columns = _columns(database, "capture_events")
    assert {"secret_policy", "derivative_policy"} <= profile_columns
    assert {"screening_decision", "screening_reasons", "derivative_policy"} <= event_columns

    provenance_columns = {"provenance", "project_identity", "source_commit", "branch_ref"}
    assert provenance_columns <= _columns(database, "capture_sessions")
    assert provenance_columns <= _columns(database, "capture_events")

    _alembic(project_root, database, "downgrade", "0009")
    assert provenance_columns.isdisjoint(_columns(database, "capture_sessions"))
    assert provenance_columns.isdisjoint(_columns(database, "capture_events"))

    _alembic(project_root, database, "upgrade", "head")
    assert provenance_columns <= _columns(database, "capture_sessions")
    assert provenance_columns <= _columns(database, "capture_events")
    assert {"secret_policy", "derivative_policy"} <= _columns(database, "capture_profiles")
    assert {"screening_decision", "screening_reasons", "derivative_policy"} <= _columns(
        database, "capture_events"
    )

    _alembic(project_root, database, "downgrade", "0008")
    assert _capture_tables(database) == set()

    _alembic(project_root, database, "upgrade", "head")
    assert _capture_tables(database) == expected
