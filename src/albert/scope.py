"""Which workspaces a read covers.

A record with no workspace is organization-wide: it is shared by every
workspace in its organization. Reading inside a workspace therefore covers
that workspace's own records plus the shared ones; reading with no workspace
covers the whole organization.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import or_, true


def in_workspace(column: Any, workspace_id: UUID | None) -> Any:
    """A SQL condition: the workspace's own rows and the shared ones, or every row for None."""
    if workspace_id is None:
        return true()
    return or_(column == workspace_id, column.is_(None))


def covers(workspace_id: UUID | None, record_workspace_id: UUID | None) -> bool:
    """Whether a read scoped to workspace_id may see a record in record_workspace_id."""
    return workspace_id is None or record_workspace_id in (None, workspace_id)
