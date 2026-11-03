"""Runtime compatibility manifest published by every Albert image.

Control planes read it with::

    docker run --rm --entrypoint albert-admin <image> compat --json

That invocation has no environment and no database, so this module must stay
importable and runnable with an empty environment: it imports only constant
modules (never ``albert.db`` or ``albert.config``) and reads migration metadata
from Alembic's script directory without connecting anywhere.

Rollback rule for consumers: a Brain migrated by image T can run image O iff
every revision in ``T.migrations.heads`` is in ``O.migrations.known_revisions``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import albert
from albert.capture_sdk import CONTRACT_VERSION as SDK_CAPTURE_CONTRACT_VERSION
from albert.schemas import CAPTURE_CONTRACT_VERSION, EXPORT_FORMAT_VERSION

MANIFEST_VERSION = "1"
PACKAGE_NAME = "albert-memory"
REST_API_VERSIONS = ("v1",)
MIGRATIONS_DIR_ENV = "ALBERT_MIGRATIONS_DIR"
# The production image's WORKDIR, where the Dockerfile copies alembic.ini and
# migrations/. Used when the command runs with a different working directory.
IMAGE_APP_DIR = Path("/app")


class MigrationsUnavailable(RuntimeError):
    """The image's Alembic migrations directory could not be located or read."""


def _script_location_from_ini(ini: Path) -> Path:
    from alembic.config import Config

    location = Config(str(ini)).get_main_option("script_location")
    if not location:
        raise MigrationsUnavailable(f"{ini} has no [alembic] script_location")
    location = location.replace("%(here)s", str(ini.parent))
    path = Path(location)
    # Alembic resolves a relative script_location against the working
    # directory; resolve it against the ini file so the result is cwd-independent.
    return path if path.is_absolute() else ini.parent / path


def resolve_migrations_dir() -> Path:
    """Locate the migrations directory shipped with this checkout or image.

    Order: an explicit ``ALBERT_MIGRATIONS_DIR`` (no fallback if it is wrong),
    then the ``alembic.ini`` next to the source tree (repository checkout),
    in the working directory, and in the image's ``/app``.
    """
    explicit = os.environ.get(MIGRATIONS_DIR_ENV, "").strip()
    if explicit:
        path = Path(explicit)
        if not (path / "versions").is_dir():
            raise MigrationsUnavailable(
                f"{MIGRATIONS_DIR_ENV}={explicit} is not an Alembic migrations directory"
            )
        return path

    candidates = [
        Path(__file__).resolve().parents[2] / "alembic.ini",
        Path.cwd() / "alembic.ini",
        IMAGE_APP_DIR / "alembic.ini",
    ]
    searched: list[str] = []
    for ini in dict.fromkeys(candidates):
        searched.append(str(ini))
        if not ini.is_file():
            continue
        path = _script_location_from_ini(ini)
        if (path / "versions").is_dir():
            return path
    raise MigrationsUnavailable(
        "No alembic.ini with a migrations directory found (searched: "
        + ", ".join(searched)
        + f"); set {MIGRATIONS_DIR_ENV}"
    )


def migration_revisions(migrations_dir: Path) -> tuple[list[str], list[str]]:
    """Return (sorted heads, all revisions in base-to-head order)."""
    from alembic.script import ScriptDirectory

    try:
        script = ScriptDirectory(str(migrations_dir))
        heads = sorted(script.get_heads())
        known = [rev.revision for rev in script.walk_revisions()]
    except Exception as exc:  # surface any Alembic load failure uniformly
        raise MigrationsUnavailable(f"Cannot read migrations in {migrations_dir}: {exc}") from exc
    if not heads or not known:
        raise MigrationsUnavailable(f"No migration revisions found in {migrations_dir}")
    known.reverse()  # walk_revisions yields head first
    return heads, known


def compatibility_manifest() -> dict[str, Any]:
    """Build the v1 manifest; raises MigrationsUnavailable rather than going partial."""
    heads, known = migration_revisions(resolve_migrations_dir())
    return {
        "compatibility_manifest": MANIFEST_VERSION,
        "package": PACKAGE_NAME,
        "package_version": albert.__version__,
        "contracts": {
            "rest_api": list(REST_API_VERSIONS),
            "capture": sorted({CAPTURE_CONTRACT_VERSION, SDK_CAPTURE_CONTRACT_VERSION}),
            "export_format": [EXPORT_FORMAT_VERSION],
        },
        "migrations": {"heads": heads, "known_revisions": known},
    }


def compatibility_error(code: str, message: str) -> dict[str, Any]:
    return {"compatibility_manifest": MANIFEST_VERSION, "error": {"code": code, "message": message}}
