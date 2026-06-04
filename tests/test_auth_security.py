"""Security-sensitive pure-function tests: JWT lifecycle + API-key hashing.

Silent breakage in these primitives is an auth-bypass or token-forgery vector.
No app startup, no mocks: the helpers are pure given settings.jwt_secret (the
same secret signs and verifies, so round-trips hold against whatever config
carries). Forged / wrong-secret / wrong-audience / expired tokens must be
rejected.
"""
import hashlib
from datetime import datetime, timezone, timedelta

import jwt
import pytest

from app.core.auth import (
    hash_api_key,
    generate_api_key,
    _create_access_token,
    _create_refresh_token,
    _decode_local_jwt,
    _JWT_AUDIENCE,
)
from app.core.config import get_settings


# ---------------------------------------------------------------------------
# API key hashing / generation
# ---------------------------------------------------------------------------
def test_hash_api_key_is_plain_sha256_hex():
    assert hash_api_key("trag_abc") == hashlib.sha256(b"trag_abc").hexdigest()


def test_hash_api_key_deterministic_and_collision_free():
    assert hash_api_key("k") == hash_api_key("k")
    assert hash_api_key("k") != hash_api_key("k2")


def test_generate_api_key_format_and_randomness():
    a, b = generate_api_key(), generate_api_key()
    assert a.startswith("trag_")
    assert len(a) == len("trag_") + 64        # "trag_" + 32 bytes as hex
    assert a != b                              # token_hex -> random


# ---------------------------------------------------------------------------
# JWT round-trip
# ---------------------------------------------------------------------------
def test_access_token_roundtrips_all_claims():
    c = _decode_local_jwt(_create_access_token("uid-1", "a@b.io", "admin"))
    assert (c["sub"], c["email"], c["role"], c["type"]) == \
        ("uid-1", "a@b.io", "admin", "access")
    assert c["aud"] == _JWT_AUDIENCE


def test_refresh_token_roundtrips():
    c = _decode_local_jwt(_create_refresh_token("uid-2"))
    assert c["sub"] == "uid-2"
    assert c["type"] == "refresh"


# ---------------------------------------------------------------------------
# Forgery / tampering must be rejected
# ---------------------------------------------------------------------------
def test_tampered_token_rejected():
    tok = _create_access_token("uid", "e@e.io", "user")
    forged = tok[:-2] + ("aa" if not tok.endswith("aa") else "bb")
    with pytest.raises(jwt.InvalidTokenError):
        _decode_local_jwt(forged)


def test_token_signed_with_wrong_secret_rejected():
    forged = jwt.encode(
        {"sub": "x", "email": "e", "role": "admin", "type": "access",
         "aud": _JWT_AUDIENCE,
         "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
         "iat": datetime.now(timezone.utc)},
        "attacker-secret", algorithm="HS256",
    )
    with pytest.raises(jwt.InvalidTokenError):
        _decode_local_jwt(forged)


def test_wrong_audience_rejected():
    tok = jwt.encode(
        {"sub": "x", "type": "access", "aud": "some-other-service",
         "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
         "iat": datetime.now(timezone.utc)},
        get_settings().jwt_secret, algorithm="HS256",
    )
    with pytest.raises(jwt.InvalidAudienceError):
        _decode_local_jwt(tok)


def test_expired_token_rejected():
    tok = jwt.encode(
        {"sub": "x", "type": "access", "aud": _JWT_AUDIENCE,
         "exp": datetime.now(timezone.utc) - timedelta(seconds=5),
         "iat": datetime.now(timezone.utc) - timedelta(minutes=1)},
        get_settings().jwt_secret, algorithm="HS256",
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        _decode_local_jwt(tok)
