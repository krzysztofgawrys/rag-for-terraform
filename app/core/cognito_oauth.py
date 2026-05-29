"""
MCP OAuth Authorization Server Provider backed by AWS Cognito.

Implements the OAuthAuthorizationServerProvider protocol from FastMCP.
Acts as an OAuth proxy: MCP clients authenticate with *this* server,
which delegates the actual login to Cognito's hosted UI and issues
its own JWTs.

Transient state (PKCE, auth codes, refresh tokens) lives in Redis.
Dynamic client registrations are stored in PostgreSQL for durability.
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import jwt as pyjwt
import redis.asyncio as aioredis
import structlog
from pydantic import AnyUrl
from sqlalchemy import text as sa_text
from starlette.requests import Request
from starlette.responses import RedirectResponse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from app.core.config import Settings, get_settings
from app.core.auth import (
    _extract_groups,
    _resolve_sso_role,
    _upsert_sso_user,
    hash_api_key,
)

log = structlog.get_logger("cognito_oauth")

# Redis key prefixes and TTLs
_PENDING_PREFIX = "mcp:oauth:pending:"
_PENDING_TTL = 600          # 10 min for user to complete Cognito login
_CODE_PREFIX = "mcp:oauth:code:"
_CODE_TTL = 300             # 5 min for client to exchange code
_REFRESH_PREFIX = "mcp:oauth:refresh:"
_REFRESH_TTL = 30 * 86400   # 30 days
_ACCESS_TTL = 3600           # 1 hour JWT lifetime

# Cognito JWKS cache
_cognito_jwks: dict[str, Any] = {}
_cognito_jwks_fetched: float = 0
_JWKS_TTL = 3600


async def _get_cognito_jwks(region: str, pool_id: str) -> dict[str, Any]:
    """Fetch and cache Cognito JWKS for ID token validation."""
    global _cognito_jwks, _cognito_jwks_fetched
    now = time.monotonic()
    if _cognito_jwks and now - _cognito_jwks_fetched < _JWKS_TTL:
        return _cognito_jwks

    url = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/jwks.json"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=5)
        resp.raise_for_status()
    _cognito_jwks = resp.json()
    _cognito_jwks_fetched = now
    return _cognito_jwks


def _decode_cognito_id_token(token: str, region: str, pool_id: str, jwks: dict) -> dict:
    """Decode and verify a Cognito ID token using cached JWKS."""
    from jwt import PyJWKSet
    keyset = PyJWKSet.from_dict(jwks)
    header = pyjwt.get_unverified_header(token)
    key = keyset[header["kid"]]
    issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"
    return pyjwt.decode(
        token, key.key, algorithms=["RS256"],
        issuer=issuer,
        options={"verify_aud": False},
    )


class CognitoOAuthProvider(OAuthAuthorizationServerProvider[
    AuthorizationCode, RefreshToken, AccessToken
]):
    """OAuth AS that proxies to Cognito for authentication."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cognito_authorize_url = (
            f"https://{settings.cognito_domain}/oauth2/authorize"
        )
        self._cognito_token_url = (
            f"https://{settings.cognito_domain}/oauth2/token"
        )
        self._callback_url = (
            f"{settings.mcp_oauth_issuer_url}/oauth/callback"
        )
        self._region = settings.sso_region
        self._pool_id = settings.cognito_user_pool_id

    def _redis(self) -> aioredis.Redis:
        return aioredis.from_url(self.settings.redis_url)

    # ------------------------------------------------------------------
    # Dynamic client registration
    # ------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        from app.core.vector_store import make_session_factory
        engine, SessionLocal = make_session_factory()
        try:
            async with SessionLocal() as db:
                result = await db.execute(
                    sa_text("SELECT client_info FROM mcp_oauth_clients WHERE client_id = :cid"),
                    {"cid": client_id},
                )
                row = result.first()
                if not row:
                    return None
                info = row[0]
                if isinstance(info, str):
                    info = json.loads(info)
                return OAuthClientInformationFull.model_validate(info)
        finally:
            await engine.dispose()

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        from app.core.vector_store import make_session_factory
        engine, SessionLocal = make_session_factory()
        try:
            async with SessionLocal() as db:
                await db.execute(
                    sa_text("""
                        INSERT INTO mcp_oauth_clients (client_id, client_info)
                        VALUES (:cid, :info)
                        ON CONFLICT (client_id) DO UPDATE SET client_info = EXCLUDED.client_info
                    """),
                    {"cid": client_info.client_id, "info": json.dumps(client_info.model_dump(mode="json"))},
                )
                await db.commit()
        finally:
            await engine.dispose()

    # ------------------------------------------------------------------
    # Authorization (redirect to Cognito)
    # ------------------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams,
    ) -> str:
        # Validate redirect_uri against the client's registered URIs
        requested_uri = str(params.redirect_uri)
        registered_uris = [str(u) for u in (client.redirect_uris or [])]
        if registered_uris and requested_uri not in registered_uris:
            raise ValueError(
                f"redirect_uri '{requested_uri}' not in client's registered URIs"
            )

        state = secrets.token_urlsafe(32)

        # Store the MCP client's PKCE + redirect_uri so the callback can find it
        pending = {
            "client_id": client.client_id,
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "scopes": params.scopes or [],
            "original_state": params.state,
            "resource": str(params.resource) if params.resource else None,
        }
        r = self._redis()
        try:
            await r.set(f"{_PENDING_PREFIX}{state}", json.dumps(pending), ex=_PENDING_TTL)
        finally:
            await r.aclose()

        # Redirect browser to Cognito hosted UI
        cognito_url = (
            f"{self._cognito_authorize_url}"
            f"?response_type=code"
            f"&client_id={self.settings.cognito_mcp_client_id}"
            f"&redirect_uri={self._callback_url}"
            f"&scope=openid+email+profile"
            f"&state={state}"
        )
        return cognito_url

    # ------------------------------------------------------------------
    # Authorization code handling
    # ------------------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str,
    ) -> AuthorizationCode | None:
        r = self._redis()
        try:
            raw = await r.get(f"{_CODE_PREFIX}{authorization_code}")
        finally:
            await r.aclose()
        if not raw:
            return None
        data = json.loads(raw)
        if data["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=data["scopes"],
            expires_at=data["expires_at"],
            client_id=data["client_id"],
            code_challenge=data["code_challenge"],
            redirect_uri=AnyUrl(data["redirect_uri"]),
            redirect_uri_provided_explicitly=data["redirect_uri_provided_explicitly"],
            resource=data.get("resource"),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        # Delete code (single-use)
        r = self._redis()
        try:
            await r.delete(f"{_CODE_PREFIX}{authorization_code.code}")
        finally:
            await r.aclose()

        # Retrieve user info stored alongside the code
        code_data_raw = authorization_code.model_dump()
        # User info was stored in Redis alongside the code; we need to fetch it
        # from a separate key
        r = self._redis()
        try:
            user_raw = await r.get(f"{_CODE_PREFIX}{authorization_code.code}:user")
            await r.delete(f"{_CODE_PREFIX}{authorization_code.code}:user")
        finally:
            await r.aclose()

        user_data = json.loads(user_raw) if user_raw else {}

        # Issue our own JWT
        now = datetime.now(timezone.utc)
        access_payload = {
            "sub": user_data.get("user_id", ""),
            "email": user_data.get("email", ""),
            "role": user_data.get("role", "user"),
            "client_id": client.client_id,
            "scopes": authorization_code.scopes,
            "iss": self.settings.mcp_oauth_issuer_url,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=_ACCESS_TTL)).timestamp()),
        }
        access_token = pyjwt.encode(access_payload, self.settings.jwt_secret, algorithm="HS256")

        # Issue refresh token
        refresh_str = secrets.token_urlsafe(48)
        refresh_data = {
            "client_id": client.client_id,
            "scopes": authorization_code.scopes,
            "user_id": user_data.get("user_id", ""),
            "email": user_data.get("email", ""),
            "role": user_data.get("role", "user"),
        }
        r = self._redis()
        try:
            await r.set(f"{_REFRESH_PREFIX}{refresh_str}", json.dumps(refresh_data), ex=_REFRESH_TTL)
        finally:
            await r.aclose()

        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=_ACCESS_TTL,
            refresh_token=refresh_str,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
        )

    # ------------------------------------------------------------------
    # Refresh token handling
    # ------------------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str,
    ) -> RefreshToken | None:
        r = self._redis()
        try:
            raw = await r.get(f"{_REFRESH_PREFIX}{refresh_token}")
        finally:
            await r.aclose()
        if not raw:
            return None
        data = json.loads(raw)
        if data["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=data["client_id"],
            scopes=data["scopes"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Load user data from old refresh token
        r = self._redis()
        try:
            raw = await r.get(f"{_REFRESH_PREFIX}{refresh_token.token}")
            # Delete old token (rotate)
            await r.delete(f"{_REFRESH_PREFIX}{refresh_token.token}")
        finally:
            await r.aclose()

        user_data = json.loads(raw) if raw else {}
        effective_scopes = scopes or refresh_token.scopes

        # Issue new access token
        now = datetime.now(timezone.utc)
        access_payload = {
            "sub": user_data.get("user_id", ""),
            "email": user_data.get("email", ""),
            "role": user_data.get("role", "user"),
            "client_id": client.client_id,
            "scopes": effective_scopes,
            "iss": self.settings.mcp_oauth_issuer_url,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=_ACCESS_TTL)).timestamp()),
        }
        access_token = pyjwt.encode(access_payload, self.settings.jwt_secret, algorithm="HS256")

        # Issue new refresh token
        new_refresh = secrets.token_urlsafe(48)
        refresh_data = {
            "client_id": client.client_id,
            "scopes": effective_scopes,
            "user_id": user_data.get("user_id", ""),
            "email": user_data.get("email", ""),
            "role": user_data.get("role", "user"),
        }
        r = self._redis()
        try:
            await r.set(f"{_REFRESH_PREFIX}{new_refresh}", json.dumps(refresh_data), ex=_REFRESH_TTL)
        finally:
            await r.aclose()

        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=_ACCESS_TTL,
            refresh_token=new_refresh,
            scope=" ".join(effective_scopes) if effective_scopes else None,
        )

    # ------------------------------------------------------------------
    # Access token validation (called on every MCP request)
    # ------------------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        # Backwards compat: trag_* API keys
        if token.startswith("trag_"):
            return await self._validate_api_key(token)

        # Our own JWT
        try:
            claims = pyjwt.decode(
                token, self.settings.jwt_secret, algorithms=["HS256"],
                issuer=self.settings.mcp_oauth_issuer_url,
            )
            return AccessToken(
                token=token,
                client_id=claims.get("client_id", ""),
                scopes=claims.get("scopes", []),
                expires_at=claims.get("exp"),
            )
        except pyjwt.ExpiredSignatureError:
            log.debug("mcp_oauth_token_expired")
            return None
        except pyjwt.InvalidTokenError:
            log.debug("mcp_oauth_token_invalid")
            return None

    async def _validate_api_key(self, token: str) -> AccessToken | None:
        """Validate a trag_* API key against the database."""
        key_hash = hash_api_key(token)
        from app.core.vector_store import make_session_factory
        engine, SessionLocal = make_session_factory()
        try:
            async with SessionLocal() as db:
                result = await db.execute(
                    sa_text("""
                        SELECT ak.id AS key_id, ak.role, ak.expires_at, ak.is_active,
                               u.is_active AS user_active, u.email
                        FROM api_keys ak JOIN users u ON ak.user_id = u.id
                        WHERE ak.key_hash = :hash
                    """),
                    {"hash": key_hash},
                )
                row = result.mappings().first()
                if not row or not row["is_active"] or not row["user_active"]:
                    return None
                if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
                    return None
                # Touch last_used
                await db.execute(
                    sa_text("UPDATE api_keys SET last_used = NOW() WHERE id = :id"),
                    {"id": row["key_id"]},
                )
                await db.commit()
                structlog.contextvars.bind_contextvars(user_email=row["email"])
                return AccessToken(
                    token=token,
                    client_id="api_key",
                    scopes=["openid", "email", "profile"],
                )
        finally:
            await engine.dispose()

    # ------------------------------------------------------------------
    # Token revocation
    # ------------------------------------------------------------------

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        r = self._redis()
        try:
            # Try both prefixes - we don't know the token type
            await r.delete(f"{_REFRESH_PREFIX}{token.token}")
        finally:
            await r.aclose()

    # ------------------------------------------------------------------
    # Cognito callback (registered as custom route on FastMCP)
    # ------------------------------------------------------------------

    def register_callback_route(self, mcp: Any) -> None:
        """Register the /oauth/callback route on the FastMCP app."""
        provider = self

        @mcp.custom_route("/oauth/callback", methods=["GET"])
        async def cognito_callback(request: Request):
            return await provider._handle_cognito_callback(request)

    async def _handle_cognito_callback(self, request: Request) -> RedirectResponse:
        """Handle Cognito redirect after user login.

        1. Exchange Cognito code for tokens
        2. Validate ID token, extract user info
        3. JIT-provision user
        4. Generate our own auth code
        5. Redirect to MCP client's redirect_uri
        """
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        error = request.query_params.get("error")

        if error:
            log.warning("cognito_callback_error", error=error,
                        description=request.query_params.get("error_description"))
            return RedirectResponse("/?error=cognito_auth_failed", status_code=302)

        if not code or not state:
            return RedirectResponse("/?error=missing_params", status_code=302)

        # Look up pending state
        r = self._redis()
        try:
            raw = await r.get(f"{_PENDING_PREFIX}{state}")
            await r.delete(f"{_PENDING_PREFIX}{state}")
        finally:
            await r.aclose()

        if not raw:
            log.warning("cognito_callback_state_not_found", state=state)
            return RedirectResponse("/?error=invalid_state", status_code=302)

        pending = json.loads(raw)

        # Exchange Cognito code for tokens
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self._cognito_token_url,
                    data={
                        "grant_type": "authorization_code",
                        "client_id": self.settings.cognito_mcp_client_id,
                        "client_secret": self.settings.cognito_mcp_client_secret,
                        "code": code,
                        "redirect_uri": self._callback_url,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=10,
                )
                resp.raise_for_status()
                cognito_tokens = resp.json()
        except Exception:
            log.exception("cognito_token_exchange_failed")
            return RedirectResponse("/?error=token_exchange_failed", status_code=302)

        # Validate Cognito ID token and extract user info
        try:
            jwks = await _get_cognito_jwks(self._region, self._pool_id)
            id_claims = _decode_cognito_id_token(
                cognito_tokens["id_token"], self._region, self._pool_id, jwks,
            )
        except Exception:
            log.exception("cognito_id_token_validation_failed")
            return RedirectResponse("/?error=id_token_invalid", status_code=302)

        email = id_claims.get("email", "")
        sub = id_claims.get("sub", "")
        display_name = id_claims.get("name") or id_claims.get("email", "")

        # Extract groups from ID token or access token
        groups = _extract_groups(id_claims)
        if not groups and "access_token" in cognito_tokens:
            try:
                access_claims = pyjwt.decode(
                    cognito_tokens["access_token"], options={"verify_signature": False},
                )
                groups = _extract_groups(access_claims)
            except Exception:
                pass

        role = _resolve_sso_role(groups)

        # JIT-provision user
        from app.core.vector_store import make_session_factory
        engine, SessionLocal = make_session_factory()
        try:
            async with SessionLocal() as db:
                user = await _upsert_sso_user(db, sub, email, role, display_name, groups)
        finally:
            await engine.dispose()

        user_id = str(user["id"])
        log.info("cognito_oauth_user_authenticated", email=email, role=role, user_id=user_id)

        # Generate our own authorization code
        our_code = secrets.token_urlsafe(32)
        code_data = {
            "client_id": pending["client_id"],
            "code_challenge": pending["code_challenge"],
            "redirect_uri": pending["redirect_uri"],
            "redirect_uri_provided_explicitly": pending["redirect_uri_provided_explicitly"],
            "scopes": pending["scopes"],
            "resource": pending.get("resource"),
            "expires_at": time.time() + _CODE_TTL,
        }
        user_data = {
            "user_id": user_id,
            "email": email,
            "role": role,
        }

        r = self._redis()
        try:
            await r.set(f"{_CODE_PREFIX}{our_code}", json.dumps(code_data), ex=_CODE_TTL)
            await r.set(f"{_CODE_PREFIX}{our_code}:user", json.dumps(user_data), ex=_CODE_TTL)
        finally:
            await r.aclose()

        # Redirect to MCP client's redirect_uri with our code
        redirect_url = construct_redirect_uri(
            pending["redirect_uri"],
            code=our_code,
            state=pending.get("original_state"),
        )
        return RedirectResponse(url=redirect_url, status_code=302)
