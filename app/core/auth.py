"""
Authentication & authorization layer.

Supports three modes (controlled by ``auth_mode`` setting):
- **disabled** — all requests pass through as anonymous (default)
- **local**    — email + password login, JWT access/refresh tokens
- **sso**      — ALB-terminated OIDC (x-amzn-oidc-data JWT header)

API keys (``trag_*``) work in all modes for programmatic access (MCP, CI/CD).
"""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import jwt
import structlog
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.vector_store import get_db

log = structlog.get_logger("auth")

# ---------------------------------------------------------------------------
# Authenticated user model
# ---------------------------------------------------------------------------

class AuthenticatedUser(BaseModel):
    id: UUID
    email: str
    role: str  # "admin", "user", "readonly"
    display_name: str | None = None
    auth_method: str  # "sso", "local", "api_key", "anonymous"


_ANONYMOUS = AuthenticatedUser(
    id=UUID("00000000-0000-0000-0000-000000000000"),
    email="anonymous",
    role="readonly",
    display_name="Anonymous",
    auth_method="anonymous",
)

# ---------------------------------------------------------------------------
# ALB OIDC public key cache  (SSO mode)
# ---------------------------------------------------------------------------

_alb_key_cache: dict[str, tuple[Any, float]] = {}  # kid → (key, fetched_at)
_ALB_KEY_TTL = 3600  # 1 hour


async def _get_alb_public_key(kid: str, region: str) -> Any:
    """Fetch (and cache) the ALB public key for *kid*."""
    now = time.monotonic()
    cached = _alb_key_cache.get(kid)
    if cached and now - cached[1] < _ALB_KEY_TTL:
        return cached[0]

    import httpx
    url = f"https://public-keys.auth.elb.{region}.amazonaws.com/{kid}"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=5)
        resp.raise_for_status()

    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    key = load_pem_public_key(resp.content)
    _alb_key_cache[kid] = (key, now)
    return key


# ---------------------------------------------------------------------------
# Token helpers (local mode)
# ---------------------------------------------------------------------------

def _create_access_token(user_id: str, email: str, role: str) -> str:
    settings = get_settings()
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_access_ttl_minutes),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def _create_refresh_token(user_id: str) -> str:
    settings = get_settings()
    payload = {
        "sub": user_id,
        "type": "refresh",
        "exp": datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_ttl_days),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def _decode_local_jwt(token: str) -> dict:
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])


# ---------------------------------------------------------------------------
# API key helpers
# ---------------------------------------------------------------------------

def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_api_key() -> str:
    return "trag_" + secrets.token_hex(32)


# ---------------------------------------------------------------------------
# User JIT provisioning  (SSO)
# ---------------------------------------------------------------------------

async def _upsert_sso_user(
    db: AsyncSession, external_id: str, email: str, role: str,
    display_name: str | None, groups: list[str],
) -> dict:
    result = await db.execute(
        sa_text("""
            INSERT INTO users (external_id, email, display_name, role, sso_groups, last_seen)
            VALUES (:ext, :email, :name, :role, :groups, NOW())
            ON CONFLICT (external_id) DO UPDATE SET
                email        = EXCLUDED.email,
                display_name = EXCLUDED.display_name,
                role         = EXCLUDED.role,
                sso_groups   = EXCLUDED.sso_groups,
                last_seen    = NOW()
            RETURNING id, email, role, display_name
        """),
        {"ext": external_id, "email": email, "name": display_name,
         "role": role, "groups": groups},
    )
    await db.commit()
    return dict(result.mappings().first())


# ---------------------------------------------------------------------------
# SSO helpers


def _extract_groups(claims: dict) -> list[str]:
    """Extract groups from ALB OIDC claims.

    Identity Center puts groups in ``custom:groups`` (comma-separated string).
    Cognito puts them in ``cognito:groups`` (list or space-separated string).
    """
    raw = claims.get("custom:groups") or claims.get("cognito:groups") or ""
    if isinstance(raw, list):
        return [g.strip() for g in raw if g.strip()]
    return [g.strip() for g in raw.replace(",", " ").split() if g.strip()]


# ---------------------------------------------------------------------------
# SSO role resolution
# ---------------------------------------------------------------------------

def _resolve_sso_role(groups: list[str]) -> str:
    settings = get_settings()
    admin_groups = {g.strip() for g in settings.sso_admin_groups.split(",") if g.strip()}
    user_groups = {g.strip() for g in settings.sso_user_groups.split(",") if g.strip()}

    if admin_groups & set(groups):
        return "admin"
    if user_groups & set(groups):
        return "user"
    # ALB OIDC + Cognito: userinfo endpoint doesn't return groups,
    # so default to sso_default_role (default: "user") instead of "readonly".
    return settings.sso_default_role


# ---------------------------------------------------------------------------
# Core FastAPI dependency: get_current_user
# ---------------------------------------------------------------------------

async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AuthenticatedUser:
    settings = get_settings()

    if settings.auth_mode == "disabled":
        return _ANONYMOUS

    # 1. ALB OIDC JWT (SSO mode)
    alb_token = request.headers.get("x-amzn-oidc-data")
    if alb_token and settings.auth_mode == "sso":
        try:
            header = jwt.get_unverified_header(alb_token)
            kid = header["kid"]
            pub_key = await _get_alb_public_key(kid, settings.sso_region)
            claims = jwt.decode(alb_token, pub_key, algorithms=["ES256"])
            # x-amzn-oidc-data (from userinfo) usually lacks groups.
            # x-amzn-oidc-accesstoken (Cognito access token) has cognito:groups.
            groups = _extract_groups(claims)
            if not groups:
                access_token = request.headers.get("x-amzn-oidc-accesstoken", "")
                if access_token:
                    try:
                        access_claims = jwt.decode(access_token, options={"verify_signature": False})
                        groups = _extract_groups(access_claims)
                    except Exception:
                        log.warning("sso_access_token_decode_failed", exc_info=True)
            role = _resolve_sso_role(groups)
            user = await _upsert_sso_user(
                db, claims["sub"], claims.get("email", ""),
                role, claims.get("name"), groups,
            )
            return AuthenticatedUser(
                id=user["id"], email=user["email"], role=user["role"],
                display_name=user["display_name"], auth_method="sso",
            )
        except Exception:
            log.warning("sso_jwt_validation_failed", exc_info=True)
            raise HTTPException(401, "Invalid SSO token")

    # 2. Authorization: Bearer header (API key or local JWT)
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]

        # 2a. API key (trag_*)
        if token.startswith("trag_"):
            key_hash = hash_api_key(token)
            result = await db.execute(
                sa_text("""
                    SELECT ak.id AS key_id, ak.role, ak.expires_at, ak.is_active,
                           u.id AS user_id, u.email, u.display_name, u.is_active AS user_active
                    FROM api_keys ak JOIN users u ON ak.user_id = u.id
                    WHERE ak.key_hash = :hash
                """),
                {"hash": key_hash},
            )
            row = result.mappings().first()
            if not row or not row["is_active"] or not row["user_active"]:
                raise HTTPException(401, "Invalid or inactive API key")
            if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
                raise HTTPException(401, "API key expired")
            # Touch last_used
            await db.execute(
                sa_text("UPDATE api_keys SET last_used = NOW() WHERE id = :id"),
                {"id": row["key_id"]},
            )
            await db.commit()
            return AuthenticatedUser(
                id=row["user_id"], email=row["email"], role=row["role"],
                display_name=row["display_name"], auth_method="api_key",
            )

        # 2b. Local JWT
        if settings.auth_mode == "local":
            try:
                claims = _decode_local_jwt(token)
                if claims.get("type") != "access":
                    raise HTTPException(401, "Invalid token type")
                return AuthenticatedUser(
                    id=UUID(claims["sub"]), email=claims["email"],
                    role=claims["role"], auth_method="local",
                )
            except jwt.ExpiredSignatureError:
                raise HTTPException(401, "Token expired")
            except jwt.InvalidTokenError:
                raise HTTPException(401, "Invalid token")

    raise HTTPException(401, "Authentication required")


# ---------------------------------------------------------------------------
# Role-checking dependencies
# ---------------------------------------------------------------------------

def require_role(*roles: str):
    async def _check(user: AuthenticatedUser = Depends(get_current_user)):
        if user.role not in roles:
            raise HTTPException(403, f"Role '{user.role}' insufficient; requires one of {roles}")
        return user
    return Depends(_check)


require_admin = require_role("admin")
require_user = require_role("admin", "user")
require_reader = require_role("admin", "user", "readonly")


# ---------------------------------------------------------------------------
# ASGI middleware for MCP sub-app auth
# ---------------------------------------------------------------------------

class AuthMiddleware:
    """ASGI middleware that validates auth before forwarding to the wrapped app.

    Used to protect the MCP Streamable-HTTP mount which doesn't use FastAPI
    dependency injection.
    """

    def __init__(self, app: Any, min_role: str = "user") -> None:
        self.app = app
        if min_role == "admin":
            self.allowed_roles = {"admin"}
        elif min_role == "readonly":
            self.allowed_roles = {"admin", "user", "readonly"}
        else:
            self.allowed_roles = {"admin", "user"}

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        # MCP spec OAuth discovery: return 404 so clients know OAuth is not
        # supported and fall back to Bearer token auth.
        path = scope.get("path", "")
        if path.startswith("/.well-known/"):
            await _send_404(send)
            return

        settings = get_settings()
        if settings.auth_mode == "disabled":
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers", []))

        # Try API key from Authorization header
        auth = headers.get(b"authorization", b"").decode()
        if auth.startswith("Bearer trag_"):
            token = auth[7:]
            key_hash = hash_api_key(token)
            # Validate key directly with a fresh DB session
            from app.core.vector_store import make_session_factory
            engine, SessionLocal = make_session_factory()
            try:
                async with SessionLocal() as db:
                    result = await db.execute(
                        sa_text("""
                            SELECT ak.role, ak.expires_at, ak.is_active,
                                   u.is_active AS user_active, u.email
                            FROM api_keys ak JOIN users u ON ak.user_id = u.id
                            WHERE ak.key_hash = :hash
                        """),
                        {"hash": key_hash},
                    )
                    row = result.mappings().first()
                    if (row and row["is_active"] and row["user_active"]
                            and row["role"] in self.allowed_roles
                            and (not row["expires_at"]
                                 or row["expires_at"] >= datetime.now(timezone.utc))):
                        # Bind user context for audit logging
                        structlog.contextvars.bind_contextvars(
                            user_email=row["email"],
                        )
                        return await self.app(scope, receive, send)
            finally:
                await engine.dispose()

        # SSO: check ALB header
        alb_token = headers.get(b"x-amzn-oidc-data", b"").decode()
        if alb_token and settings.auth_mode == "sso":
            try:
                header = jwt.get_unverified_header(alb_token)
                pub_key = await _get_alb_public_key(header["kid"], settings.sso_region)
                claims = jwt.decode(alb_token, pub_key, algorithms=["ES256"])
                groups = _extract_groups(claims)
                if not groups:
                    access_token = headers.get(b"x-amzn-oidc-accesstoken", b"").decode()
                    if access_token:
                        try:
                            access_claims = jwt.decode(access_token, options={"verify_signature": False})
                            groups = _extract_groups(access_claims)
                        except Exception:
                            pass
                role = _resolve_sso_role(groups)
                if role in self.allowed_roles:
                    structlog.contextvars.bind_contextvars(
                        user_email=claims.get("email", ""),
                    )
                    return await self.app(scope, receive, send)
            except Exception:
                pass

        # Reject
        await _send_401(send)

    async def _send_json(self, send: Any, status: int, body: bytes) -> None:
        await send({"type": "http.response.start", "status": status,
                     "headers": [[b"content-type", b"application/json"]]})
        await send({"type": "http.response.body", "body": body})


async def _send_404(send: Any) -> None:
    await send({
        "type": "http.response.start", "status": 404,
        "headers": [[b"content-type", b"application/json"]],
    })
    await send({
        "type": "http.response.body",
        "body": b'{"detail":"Not found"}',
    })


async def _send_401(send: Any) -> None:
    await send({
        "type": "http.response.start", "status": 401,
        "headers": [[b"content-type", b"application/json"]],
    })
    await send({
        "type": "http.response.body",
        "body": b'{"detail":"Authentication required"}',
    })


# ---------------------------------------------------------------------------
# Seed admin (local mode, runs once on startup)
# ---------------------------------------------------------------------------

async def seed_initial_admin() -> None:
    """Create the initial admin user if configured and not already present."""
    settings = get_settings()
    if settings.auth_mode != "local":
        return
    if not settings.admin_initial_email or not settings.admin_initial_password:
        return

    import bcrypt
    from app.core.vector_store import make_session_factory
    engine, SessionLocal = make_session_factory()
    try:
        async with SessionLocal() as db:
            existing = await db.execute(
                sa_text("SELECT id FROM users WHERE email = :email"),
                {"email": settings.admin_initial_email},
            )
            if existing.first():
                return
            pw_hash = bcrypt.hashpw(
                settings.admin_initial_password.encode(), bcrypt.gensalt(rounds=12)
            ).decode()
            await db.execute(
                sa_text("""
                    INSERT INTO users (email, display_name, password_hash, role)
                    VALUES (:email, 'Admin', :pw, 'admin')
                """),
                {"email": settings.admin_initial_email, "pw": pw_hash},
            )
            await db.commit()
            log.info("seed_admin_created", email=settings.admin_initial_email)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Seed demo user (runs once on startup when DEMO_MODE is set)
# ---------------------------------------------------------------------------

async def seed_demo_user() -> None:
    """Create the demo readonly user when DEMO_MODE=true."""
    settings = get_settings()
    if not settings.demo_mode:
        return
    if settings.auth_mode != "local":
        return

    import bcrypt
    from app.core.vector_store import make_session_factory
    engine, SessionLocal = make_session_factory()
    try:
        async with SessionLocal() as db:
            existing = await db.execute(
                sa_text("SELECT id FROM users WHERE email = :email"),
                {"email": "demo@terraform-rag.io"},
            )
            if existing.first():
                return
            pw_hash = bcrypt.hashpw(b"demo", bcrypt.gensalt(rounds=12)).decode()
            await db.execute(
                sa_text("""
                    INSERT INTO users (email, display_name, password_hash, role)
                    VALUES ('demo@terraform-rag.io', 'Demo User', :pw, 'readonly')
                """),
                {"pw": pw_hash},
            )
            await db.commit()
            log.info("seed_demo_user_created")
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Seed MCP API key (runs once on startup when MCP_SEED_API_KEY is set)
# ---------------------------------------------------------------------------

async def seed_mcp_api_key() -> None:
    """Insert the pre-generated MCP API key if not already present.

    The key comes from ``MCP_SEED_API_KEY`` env var (set via Secrets Manager
    on AWS).  It is hashed and stored in ``api_keys`` so that MCP clients
    can authenticate with ``Authorization: Bearer trag_...``.
    """
    settings = get_settings()
    raw_key = settings.mcp_seed_api_key
    if not raw_key or not raw_key.startswith("trag_"):
        return

    key_hash = hash_api_key(raw_key)

    from app.core.vector_store import make_session_factory
    engine, SessionLocal = make_session_factory()
    try:
        async with SessionLocal() as db:
            existing = await db.execute(
                sa_text("SELECT id FROM api_keys WHERE key_hash = :hash"),
                {"hash": key_hash},
            )
            if existing.first():
                return

            # Find the admin user to own this key (seed admin or first admin)
            admin = await db.execute(
                sa_text("SELECT id FROM users WHERE role = 'admin' ORDER BY first_seen LIMIT 1"),
            )
            admin_row = admin.first()
            if not admin_row:
                log.warning("seed_mcp_api_key_skipped", reason="no admin user exists yet")
                return

            key_prefix = raw_key[:13]  # "trag_" + first 8 hex chars
            await db.execute(
                sa_text("""
                    INSERT INTO api_keys (user_id, name, key_hash, key_prefix, role)
                    VALUES (:uid, 'mcp-seed', :hash, :prefix, 'user')
                """),
                {"uid": admin_row[0], "hash": key_hash, "prefix": key_prefix},
            )
            await db.commit()
            log.info("seed_mcp_api_key_created", key_prefix=key_prefix)
    finally:
        await engine.dispose()
