"""SSRF guard on git clone URLs - indexer._validate_repo_url.

This is the single choke-point every clone path (webhook, /index/, reindex,
consumer) runs through. It must block bad protocols, loopback / link-local /
RFC-1918 + cloud-metadata hosts, and - when an allowlist is configured - any
host outside it (the positive control the /index/ route previously lacked).
"""
import pytest

import app.services.indexer as indexer
from app.services.indexer import _validate_repo_url


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch):
    monkeypatch.setattr(indexer.settings, "webhook_allowed_hosts", "github.com,gitlab.com")


@pytest.mark.parametrize("url", [
    "https://github.com/org/repo.git",
    "git@github.com:org/repo.git",
    "https://gitlab.com/org/repo.git",
])
def test_allowlisted_hosts_pass(url):
    _validate_repo_url(url)   # no raise == accepted


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://host/repo",
    "https://169.254.169.254/latest/meta-data/",   # cloud metadata
    "https://10.0.0.5/x.git",                       # RFC-1918
    "https://192.168.1.1/x.git",
    "https://172.16.5.1/x.git",                     # private 172.16/12
    "git@127.0.0.1:x.git",
    "https://localhost/x.git",
    "https://evil.example.com/x.git",               # not in allowlist
    "https://172.32.0.1/x.git",                     # public 172.x, still not allowlisted
])
def test_dangerous_or_non_allowlisted_blocked(url):
    with pytest.raises(ValueError):
        _validate_repo_url(url)


def test_private_172_blocked_but_public_172_allowed_if_listed(monkeypatch):
    # 172.16-31 is RFC-1918 (always blocked); 172.32 is public (allowed only
    # when explicitly allowlisted).
    monkeypatch.setattr(indexer.settings, "webhook_allowed_hosts", "172.16.5.1,172.32.0.1")
    with pytest.raises(ValueError):
        _validate_repo_url("https://172.16.5.1/x.git")   # private, blocklist wins
    _validate_repo_url("https://172.32.0.1/x.git")        # public + allowlisted


def test_empty_allowlist_falls_back_to_blocklist_only(monkeypatch):
    monkeypatch.setattr(indexer.settings, "webhook_allowed_hosts", "")
    _validate_repo_url("https://anything.example.com/x.git")     # no allowlist -> blocklist only
    with pytest.raises(ValueError):
        _validate_repo_url("https://169.254.169.254/x")          # metadata still blocked
