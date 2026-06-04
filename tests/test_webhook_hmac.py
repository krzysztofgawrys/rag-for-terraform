"""GitHub webhook HMAC verification (security-sensitive).

_verify_github_signature is the gate stopping an unauthenticated POST from
triggering a clone+index of an arbitrary repo (an SSRF/RCE-shaped risk). It must
accept ONLY a correct `sha256=` HMAC of the exact body under the configured
secret, and reject missing / malformed / wrong-secret / wrong-body signatures.
The function uses hmac.compare_digest; we assert behaviour, not timing.
"""
import hashlib
import hmac

import pytest
from fastapi import HTTPException

import app.api.routes.webhook as webhook

SECRET = "s3cr3t-webhook-key"


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    monkeypatch.setattr(webhook.settings, "github_webhook_secret", SECRET)


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature_accepted():
    body = b'{"ref":"refs/heads/main"}'
    webhook._verify_github_signature(body, _sign(body))   # no raise == accepted


def test_missing_signature_rejected():
    with pytest.raises(HTTPException) as ei:
        webhook._verify_github_signature(b"{}", None)
    assert ei.value.status_code == 401


def test_wrong_secret_signature_rejected():
    body = b'{"ref":"x"}'
    with pytest.raises(HTTPException) as ei:
        webhook._verify_github_signature(body, _sign(body, "attacker-secret"))
    assert ei.value.status_code == 401


def test_tampered_body_rejected():
    sig_for_main = _sign(b'{"ref":"main"}')
    with pytest.raises(HTTPException) as ei:
        webhook._verify_github_signature(b'{"ref":"evil"}', sig_for_main)
    assert ei.value.status_code == 401


def test_malformed_signature_header_rejected():
    # missing the "sha256=" framing entirely
    with pytest.raises(HTTPException):
        webhook._verify_github_signature(b"{}", "deadbeef")


def test_exact_body_bytes_matter(monkeypatch):
    # a single trailing byte difference must fail (HMAC is over raw bytes)
    body = b'{"a":1}'
    sig = _sign(body)
    with pytest.raises(HTTPException):
        webhook._verify_github_signature(body + b"\n", sig)
