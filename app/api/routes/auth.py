"""
Auth routes — login, token refresh, user info, API key management.
"""
from __future__ import annotations

from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.vector_store import get_db
from app.core.auth import (
    AuthenticatedUser, get_current_user, require_admin, require_user,
    _create_access_token, _create_refresh_token, _decode_local_jwt,
    generate_api_key, hash_api_key,
)

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int

class ApiKeyCreate(BaseModel):
    name: str
    role: str = "user"
    expires_in_days: int | None = None

class ApiKeyResponse(BaseModel):
    id: str
    name: str
    key_prefix: str
    role: str
    created_at: str
    expires_at: str | None = None
    last_used: str | None = None
    is_active: bool

class ApiKeyCreated(ApiKeyResponse):
    key: str  # plaintext, shown once


class AuthInfoResponse(BaseModel):
    auth_mode: str


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

@router.get("/info")
async def auth_info():
    """Return auth mode so frontend knows which login flow to show."""
    settings = get_settings()
    return AuthInfoResponse(auth_mode=settings.auth_mode)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    """Email + password login (local mode only). Returns access token; sets refresh cookie."""
    settings = get_settings()
    if settings.auth_mode != "local":
        raise HTTPException(400, "Login endpoint only available in local auth mode")

    result = await db.execute(
        text("SELECT id, email, role, password_hash, is_active FROM users WHERE email = :email"),
        {"email": body.email},
    )
    user = result.mappings().first()
    if not user or not user["password_hash"]:
        raise HTTPException(401, "Invalid credentials")
    if not user["is_active"]:
        raise HTTPException(403, "Account disabled")

    import bcrypt
    if not bcrypt.checkpw(body.password.encode(), user["password_hash"].encode()):
        raise HTTPException(401, "Invalid credentials")

    # Update last_seen
    await db.execute(text("UPDATE users SET last_seen = NOW() WHERE id = :id"), {"id": user["id"]})
    await db.commit()

    access = _create_access_token(str(user["id"]), user["email"], user["role"])
    refresh = _create_refresh_token(str(user["id"]))

    response.set_cookie(
        key="refresh_token", value=refresh,
        httponly=True, samesite="lax", secure=False,  # secure=True in prod behind HTTPS
        max_age=settings.jwt_refresh_ttl_days * 86400,
        path="/auth/refresh",
    )

    return TokenResponse(
        access_token=access,
        expires_in=settings.jwt_access_ttl_minutes * 60,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    response: Response,
    db: AsyncSession = Depends(get_db),
    # Refresh token from cookie — FastAPI doesn't have a built-in cookie Depends,
    # so we read it from the request via a sub-dependency.
):
    """Exchange refresh token (from cookie) for a new access token."""
    from starlette.requests import Request
    from fastapi import Request as FRequest

    # This is a workaround — we need the raw request to read cookies
    raise HTTPException(501, "Use /auth/refresh-token endpoint with cookie")


@router.post("/refresh-token", response_model=TokenResponse)
async def refresh_token_from_body(
    response: Response,
    db: AsyncSession = Depends(get_db),
    refresh_token: str | None = None,
):
    """Exchange refresh token for new access + refresh tokens.

    Accepts refresh_token as JSON body field or reads from cookie.
    """
    settings = get_settings()
    if settings.auth_mode != "local":
        raise HTTPException(400, "Only available in local auth mode")

    if not refresh_token:
        raise HTTPException(401, "Missing refresh token")

    try:
        claims = _decode_local_jwt(refresh_token)
    except Exception:
        raise HTTPException(401, "Invalid refresh token")

    if claims.get("type") != "refresh":
        raise HTTPException(401, "Invalid token type")

    user_id = claims["sub"]
    result = await db.execute(
        text("SELECT id, email, role, is_active FROM users WHERE id = :id"),
        {"id": user_id},
    )
    user = result.mappings().first()
    if not user or not user["is_active"]:
        raise HTTPException(401, "User not found or disabled")

    access = _create_access_token(str(user["id"]), user["email"], user["role"])
    new_refresh = _create_refresh_token(str(user["id"]))

    response.set_cookie(
        key="refresh_token", value=new_refresh,
        httponly=True, samesite="lax", secure=False,
        max_age=settings.jwt_refresh_ttl_days * 86400,
        path="/auth/refresh",
    )

    return TokenResponse(
        access_token=access,
        expires_in=settings.jwt_access_ttl_minutes * 60,
    )


# ---------------------------------------------------------------------------
# Authenticated
# ---------------------------------------------------------------------------

@router.get("/me")
async def get_me(user: AuthenticatedUser = Depends(get_current_user)):
    """Current user info."""
    return {
        "id": str(user.id),
        "email": user.email,
        "role": user.role,
        "display_name": user.display_name,
        "auth_method": user.auth_method,
    }


@router.post("/logout")
async def logout(response: Response):
    """Clear refresh token cookie."""
    response.delete_cookie("refresh_token", path="/auth/refresh")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# API key management (admin only)
# ---------------------------------------------------------------------------

@router.get("/api-keys", response_model=list[ApiKeyResponse])
async def list_api_keys(
    user: AuthenticatedUser = require_admin,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(text("""
        SELECT id, name, key_prefix, role, created_at, expires_at, last_used, is_active
        FROM api_keys ORDER BY created_at DESC
    """))
    rows = result.mappings().all()
    return [
        ApiKeyResponse(
            id=str(r["id"]), name=r["name"], key_prefix=r["key_prefix"],
            role=r["role"],
            created_at=r["created_at"].isoformat() if r["created_at"] else "",
            expires_at=r["expires_at"].isoformat() if r["expires_at"] else None,
            last_used=r["last_used"].isoformat() if r["last_used"] else None,
            is_active=r["is_active"],
        )
        for r in rows
    ]


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=201)
async def create_api_key(
    body: ApiKeyCreate,
    user: AuthenticatedUser = require_admin,
    db: AsyncSession = Depends(get_db),
):
    """Create a new API key. The plaintext key is returned ONCE."""
    if body.role not in ("admin", "user", "readonly"):
        raise HTTPException(400, "Invalid role")

    raw_key = generate_api_key()
    key_hash = hash_api_key(raw_key)
    key_prefix = raw_key[:13]  # "trag_" + first 8 hex chars

    expires_at = None
    if body.expires_in_days:
        from datetime import datetime, timedelta, timezone
        expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)

    result = await db.execute(
        text("""
            INSERT INTO api_keys (user_id, name, key_hash, key_prefix, role, expires_at)
            VALUES (:uid, :name, :hash, :prefix, :role, :expires)
            RETURNING id, created_at
        """),
        {
            "uid": user.id, "name": body.name, "hash": key_hash,
            "prefix": key_prefix, "role": body.role, "expires": expires_at,
        },
    )
    row = result.mappings().first()
    await db.commit()

    return ApiKeyCreated(
        id=str(row["id"]), name=body.name, key_prefix=key_prefix,
        role=body.role,
        created_at=row["created_at"].isoformat(),
        expires_at=expires_at.isoformat() if expires_at else None,
        last_used=None, is_active=True,
        key=raw_key,
    )


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    key_id: UUID,
    user: AuthenticatedUser = require_admin,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        text("UPDATE api_keys SET is_active = FALSE WHERE id = :id RETURNING id"),
        {"id": key_id},
    )
    if not result.first():
        raise HTTPException(404, "API key not found")
    await db.commit()
    return {"status": "revoked", "id": str(key_id)}
