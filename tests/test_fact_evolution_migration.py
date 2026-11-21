from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

EXPECTED_INDEXES = {
    "ix_fact_evolutions_organization_id",
    "ix_fact_evolutions_workspace_id",
    "ix_fact_evolutions_principal_id",
    "ix_fact_evolutions_scope",
    "ix_fact_evolutions_evidence_memory_id",
    "ix_fact_evolutions_fact_memory_id",
    "ix_fact_evolutions_result_memory_id",
    "ix_fact_evolutions_state",
}


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


def _fact_evolution_indexes(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {
            row[1]
            for row in connection.execute("PRAGMA index_list('fact_evolutions')")
            if row[1].startswith("ix_fact_evolutions_")
        }


def test_fact_evolution_migration_indexes_round_trip(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    database = tmp_path / "fact-evolution-migration.sqlite3"

    _alembic(project_root, database, "upgrade", "0017")
    assert _fact_evolution_indexes(database) == EXPECTED_INDEXES
    _alembic(project_root, database, "upgrade", "head")
    _alembic(project_root, database, "check")

    _alembic(project_root, database, "downgrade", "0016")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'fact_evolutions'"
        ).fetchone() is None

    _alembic(project_root, database, "upgrade", "head")
    assert _fact_evolution_indexes(database) == EXPECTED_INDEXES
    _alembic(project_root, database, "check")
