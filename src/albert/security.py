from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from albert.config import get_settings
from albert.db import get_session
from albert.models import APIKey, Principal

bearer = HTTPBearer(auto_error=False)
# Writing last_used_at on every request turns a shared agent key into a hot row.
LAST_USED_WRITE_INTERVAL = timedelta(seconds=60)


@dataclass(frozen=True)
class AuthContext:
    principal_id: UUID
    organization_id: UUID
    workspace_id: UUID | None
    capabilities: frozenset[str]

    def require(self, capability: str) -> None:
        if "admin" not in self.capabilities and capability not in self.capabilities:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing capability: {capability}",
            )

    def allowed_sensitivities(self) -> list[str]:
        allowed = ["public", "internal"]
        if "admin" in self.capabilities or "memory.confidential" in self.capabilities:
            allowed.append("confidential")
        if "admin" in self.capabilities or "memory.restricted" in self.capabilities:
            allowed.append("restricted")
        return allowed


def _digest(value: str) -> bytes:
    pepper = get_settings().api_key_pepper.get_secret_value().encode()
    return hmac.new(pepper, value.encode(), hashlib.sha256).digest()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def issue_api_key() -> tuple[str, str, bytes]:
    prefix = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
    secret = secrets.token_urlsafe(32)
    value = f"alb_{prefix}_{secret}"
    return value, prefix, _digest(value)


def hash_lock_token(value: str) -> bytes:
    return _digest(f"lock:{value}")


def verify_lock_token(value: str, expected: bytes) -> bool:
    return hmac.compare_digest(hash_lock_token(value), expected)


def parse_api_key(value: str) -> tuple[str, str] | None:
    parts = value.split("_", 2)
    if len(parts) != 3 or parts[0] != "alb" or not parts[1] or not parts[2]:
        return None
    return parts[1], value


def authenticate(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: Session = Depends(get_session),
) -> AuthContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Bearer API key required")
    parsed = parse_api_key(credentials.credentials)
    if parsed is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    prefix, raw = parsed
    record = session.scalar(select(APIKey).where(APIKey.prefix == prefix, APIKey.active.is_(True)))
    now = datetime.now(UTC)
    if (
        record is None
        or not hmac.compare_digest(record.key_hash, _digest(raw))
        or (record.expires_at is not None and _as_utc(record.expires_at) <= now)
        or not record.principal.active
    ):
        raise HTTPException(status_code=401, detail="Invalid or expired API key")
    last_used = record.last_used_at
    if last_used is None or now - _as_utc(last_used) >= LAST_USED_WRITE_INTERVAL:
        record.last_used_at = now
        session.commit()
    principal: Principal = record.principal
    return AuthContext(
        principal_id=principal.id,
        organization_id=principal.organization_id,
        workspace_id=principal.workspace_id,
        capabilities=frozenset(record.capabilities or []),
    )


def require_capability(capability: str):  # type: ignore[no-untyped-def]
    def dependency(auth: AuthContext = Depends(authenticate)) -> AuthContext:
        auth.require(capability)
        return auth

    return dependency
