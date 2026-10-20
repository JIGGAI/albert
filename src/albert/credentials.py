"""Issue and revoke agent credentials: scoped API keys and single-use connection links.

Managing credentials needs keys.manage, which is never part of console.read, so
a read-only console key (the one Albert Cloud holds, for example) cannot mint
anything. These routes live outside /v1/console for the same reason: the
console's read proxies forward that prefix.

A connection link is redeemed by an agent with no credential at all, so the
/onboard routes are unauthenticated and the token is the only secret. Only its
digest is stored, redemption is a single conditional UPDATE so a link can be
used once even under concurrent redeems, and an issued key can never carry more
than the link granted.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from albert.config import get_settings
from albert.db import get_session
from albert.models import APIKey, ConnectionLink, Principal
from albert.security import (
    GRANTABLE_CAPABILITIES,
    AuthContext,
    _as_utc,
    _digest,
    issue_api_key,
    require_capability,
)
from albert.services import audit

settings_router = APIRouter(prefix="/v1/settings", tags=["settings"])
onboard_router = APIRouter(prefix="/onboard", tags=["onboarding"])

LINK_TTL_MIN_SECONDS = 60
LINK_TTL_MAX_SECONDS = 7 * 24 * 3600
LINK_TTL_DEFAULT_SECONDS = 3600

_manager = require_capability("keys.manage")


def _hash_link_token(token: str) -> bytes:
    return _digest(f"onboard:{token}")


def _iso(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None


def _validated_capabilities(value: list[str]) -> list[str]:
    requested = sorted({item.strip() for item in value if item.strip()})
    if not requested:
        raise ValueError("Choose at least one capability")
    unknown = [item for item in requested if item not in GRANTABLE_CAPABILITIES]
    if unknown:
        raise ValueError(f"Capabilities cannot be granted: {', '.join(unknown)}")
    return requested


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    capabilities: list[str] = Field(max_length=32)

    _capabilities = field_validator("capabilities")(_validated_capabilities)


class LinkCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    capabilities: list[str] = Field(max_length=32)
    expires_in_seconds: int = Field(
        default=LINK_TTL_DEFAULT_SECONDS, ge=LINK_TTL_MIN_SECONDS, le=LINK_TTL_MAX_SECONDS
    )

    _capabilities = field_validator("capabilities")(_validated_capabilities)


class Redeem(BaseModel):
    agent_name: str = Field(min_length=1, max_length=200)
    # Omitted means everything the link grants.
    capabilities: list[str] | None = Field(default=None, max_length=32)


def _mint_key(
    session: Session,
    *,
    organization_id: UUID,
    workspace_id: UUID | None,
    name: str,
    capabilities: list[str],
) -> tuple[APIKey, str]:
    """One principal per credential, so revoking or auditing one agent never touches another."""
    principal = Principal(
        organization_id=organization_id,
        workspace_id=workspace_id,
        name=name,
        principal_type="agent",
    )
    session.add(principal)
    session.flush()
    raw, prefix, digest = issue_api_key()
    key = APIKey(
        principal_id=principal.id,
        prefix=prefix,
        key_hash=digest,
        name=name,
        capabilities=capabilities,
    )
    session.add(key)
    session.flush()
    return key, raw


def _key_row(key: APIKey, auth: AuthContext) -> dict:
    return {
        "id": str(key.id),
        "name": key.name,
        "principal_name": key.principal.name,
        "prefix": key.prefix,
        "capabilities": sorted(key.capabilities or []),
        "active": key.active,
        "created_at": _iso(key.created_at),
        "last_used_at": _iso(key.last_used_at),
        "expires_at": _iso(key.expires_at),
        "current": key.principal_id == auth.principal_id,
    }


def _link_state(link: ConnectionLink, now: datetime) -> str:
    if link.revoked_at is not None:
        return "revoked"
    if link.used_at is not None:
        return "used"
    if _as_utc(link.expires_at) <= now:
        return "expired"
    return "open"


def _link_row(link: ConnectionLink, now: datetime) -> dict:
    return {
        "id": str(link.id),
        "name": link.name,
        "capabilities": sorted(link.capabilities or []),
        "state": _link_state(link, now),
        "created_at": _iso(link.created_at),
        "expires_at": _iso(link.expires_at),
        "used_at": _iso(link.used_at),
        "api_key_id": str(link.api_key_id) if link.api_key_id else None,
    }


@settings_router.get("/credentials")
def list_credentials(
    auth: AuthContext = Depends(_manager), session: Session = Depends(get_session)
) -> dict:
    now = datetime.now(UTC)
    keys = session.scalars(
        select(APIKey)
        .join(Principal, APIKey.principal_id == Principal.id)
        .where(Principal.organization_id == auth.organization_id)
        .order_by(APIKey.created_at.desc())
    )
    links = session.scalars(
        select(ConnectionLink)
        .where(ConnectionLink.organization_id == auth.organization_id)
        .order_by(ConnectionLink.created_at.desc())
    )
    return {
        "grantable_capabilities": GRANTABLE_CAPABILITIES,
        "api_keys": [_key_row(key, auth) for key in keys],
        "connection_links": [_link_row(link, now) for link in links],
        "rest_url": get_settings().public_base_url,
        "mcp_url": get_settings().public_mcp_url,
    }


@settings_router.post("/api-keys", status_code=201)
def create_key(
    payload: KeyCreate,
    auth: AuthContext = Depends(_manager),
    session: Session = Depends(get_session),
) -> dict:
    key, raw = _mint_key(
        session,
        organization_id=auth.organization_id,
        workspace_id=auth.workspace_id,
        name=payload.name,
        capabilities=payload.capabilities,
    )
    audit(
        session,
        auth,
        "api_key.create",
        resource_type="api_key",
        resource_id=str(key.id),
        detail={"name": payload.name, "capabilities": payload.capabilities},
    )
    session.commit()
    session.refresh(key)
    return {**_key_row(key, auth), "credential": raw}


@settings_router.delete("/api-keys/{key_id}", status_code=204)
def revoke_key(
    key_id: UUID, auth: AuthContext = Depends(_manager), session: Session = Depends(get_session)
) -> None:
    key = session.get(APIKey, key_id)
    if key is None or key.principal.organization_id != auth.organization_id:
        raise HTTPException(status_code=404, detail="API key not found")
    if key.principal_id == auth.principal_id:
        # Revoking the key that is making the request would lock the console out.
        raise HTTPException(status_code=409, detail="This is the key the console is using")
    key.active = False
    audit(session, auth, "api_key.revoke", resource_type="api_key", resource_id=str(key.id))
    session.commit()


@settings_router.post("/connection-links", status_code=201)
def create_link(
    payload: LinkCreate,
    auth: AuthContext = Depends(_manager),
    session: Session = Depends(get_session),
) -> dict:
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    link = ConnectionLink(
        organization_id=auth.organization_id,
        workspace_id=auth.workspace_id,
        created_by_principal_id=auth.principal_id,
        token_hash=_hash_link_token(token),
        name=payload.name,
        capabilities=payload.capabilities,
        expires_at=now + timedelta(seconds=payload.expires_in_seconds),
    )
    session.add(link)
    session.flush()
    audit(
        session,
        auth,
        "connection_link.create",
        resource_type="connection_link",
        resource_id=str(link.id),
        detail={"name": payload.name, "capabilities": payload.capabilities},
    )
    session.commit()
    session.refresh(link)
    base = get_settings().public_base_url.rstrip("/")
    return {**_link_row(link, now), "url": f"{base}/onboard/{token}"}


@settings_router.delete("/connection-links/{link_id}", status_code=204)
def revoke_link(
    link_id: UUID, auth: AuthContext = Depends(_manager), session: Session = Depends(get_session)
) -> None:
    link = session.get(ConnectionLink, link_id)
    if link is None or link.organization_id != auth.organization_id:
        raise HTTPException(status_code=404, detail="Connection link not found")
    if link.revoked_at is None and link.used_at is None:
        link.revoked_at = datetime.now(UTC)
        audit(
            session,
            auth,
            "connection_link.revoke",
            resource_type="connection_link",
            resource_id=str(link.id),
        )
        session.commit()


def _open_link(session: Session, token: str) -> ConnectionLink:
    link = session.scalar(
        select(ConnectionLink).where(ConnectionLink.token_hash == _hash_link_token(token))
    )
    if link is None or _link_state(link, datetime.now(UTC)) != "open":
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This connection link is expired, revoked, or already used",
        )
    return link


def _manifest(link: ConnectionLink, token: str) -> dict:
    config = get_settings()
    base = config.public_base_url.rstrip("/")
    return {
        "version": "1",
        "name": link.name,
        "expires_at": _iso(link.expires_at),
        "capabilities": sorted(link.capabilities or []),
        "redeem_url": f"{base}/onboard/{token}/redeem",
        "connections": {
            "mcp": {
                "transport": "streamable-http",
                "url": config.public_mcp_url,
                "authorization": "Bearer <credential>",
            },
            "rest": {"base_url": base, "auth": "Bearer <credential>"},
        },
    }


@onboard_router.get("/{token}")
@onboard_router.get("/{token}/manifest")
def manifest(token: str, session: Session = Depends(get_session)) -> dict:
    return _manifest(_open_link(session, token), token)


@onboard_router.post("/{token}/redeem", status_code=201)
def redeem(token: str, payload: Redeem, session: Session = Depends(get_session)) -> dict:
    link = _open_link(session, token)
    granted = set(link.capabilities or [])
    requested = sorted(granted if payload.capabilities is None else set(payload.capabilities))
    if not requested or not set(requested) <= granted:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"This link grants only: {', '.join(sorted(granted))}",
        )
    now = datetime.now(UTC)
    # Claim the link atomically; a concurrent redeem of the same token updates nothing.
    claimed = session.execute(
        update(ConnectionLink)
        .where(
            ConnectionLink.id == link.id,
            ConnectionLink.used_at.is_(None),
            ConnectionLink.revoked_at.is_(None),
            ConnectionLink.expires_at > now,
        )
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    if claimed != 1:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This connection link is expired, revoked, or already used",
        )
    key, raw = _mint_key(
        session,
        organization_id=link.organization_id,
        workspace_id=link.workspace_id,
        name=payload.agent_name,
        capabilities=requested,
    )
    session.execute(
        update(ConnectionLink).where(ConnectionLink.id == link.id).values(api_key_id=key.id)
    )
    audit(
        session,
        AuthContext(
            principal_id=key.principal_id,
            organization_id=link.organization_id,
            workspace_id=link.workspace_id,
            capabilities=frozenset(requested),
        ),
        "connection_link.redeem",
        resource_type="connection_link",
        resource_id=str(link.id),
        detail={"api_key_id": str(key.id), "capabilities": requested},
    )
    session.commit()
    config = get_settings()
    return {
        "agent": {"id": str(key.principal_id), "name": payload.agent_name},
        "capabilities": requested,
        "credential": raw,
        "credential_notice": "Shown once; store it in the client secret store.",
        "rest_url": config.public_base_url.rstrip("/"),
        "mcp_url": config.public_mcp_url,
    }
