"""Security utility tests (shared-store limiter + auth)."""

from __future__ import annotations

from flask import Flask, request
from itsdangerous import URLSafeTimedSerializer

from src.utils import security


def test_sqlite_rate_limiter_shared_store(tmp_path):
    db_path = tmp_path / "rate_limit.sqlite3"
    a = security.SQLiteRateLimiter(str(db_path), limit_per_minute=2)
    b = security.SQLiteRateLimiter(str(db_path), limit_per_minute=2)

    assert a.allow("1.2.3.4") is True
    assert b.allow("1.2.3.4") is True
    assert a.allow("1.2.3.4") is False


def test_resolve_client_ip_respects_proxy_flag(monkeypatch):
    app = Flask(__name__)
    with app.test_request_context("/", headers={"X-Forwarded-For": "9.9.9.9, 8.8.8.8"}):
        monkeypatch.setattr(security.config, "DASHBOARD_TRUST_PROXY", True)
        assert security.resolve_client_ip(request) == "9.9.9.9"
        monkeypatch.setattr(security.config, "DASHBOARD_TRUST_PROXY", False)
        # In test context, remote_addr may be None -> fallback "unknown"
        assert security.resolve_client_ip(request) in {"unknown", request.remote_addr or "unknown"}


def test_ui_token_verification_accepts_all_configured_tokens(monkeypatch):
    monkeypatch.setattr(security.config, "DASHBOARD_API_KEY", "")
    monkeypatch.setattr(security.config, "DASHBOARD_API_KEYS", "key1,key2")
    monkeypatch.setattr(security.config, "DASHBOARD_UI_TOKEN_TTL_SEC", 3600)

    payload = {"scope": "dashboard-ui"}
    token_key1 = URLSafeTimedSerializer(secret_key="key1", salt="dashboard-ui").dumps(payload)
    token_key2 = URLSafeTimedSerializer(secret_key="key2", salt="dashboard-ui").dumps(payload)

    assert security.is_authorized_dashboard_ui_token(token_key1) is True
    assert security.is_authorized_dashboard_ui_token(token_key2) is True


def test_xff_spoofing_leftmost_ip_used(monkeypatch):
    """With proxy trust enabled the leftmost (client) IP from X-Forwarded-For is used."""
    app = Flask(__name__)
    # Chain: client -> proxy1 -> proxy2; leftmost is the real client
    xff = "1.1.1.1, 2.2.2.2, 3.3.3.3"
    with app.test_request_context("/", headers={"X-Forwarded-For": xff}):
        monkeypatch.setattr(security.config, "DASHBOARD_TRUST_PROXY", True)
        ip = security.resolve_client_ip(request)
        assert ip == "1.1.1.1", f"Expected leftmost IP '1.1.1.1', got '{ip}'"


def test_xff_ignored_when_proxy_trust_disabled(monkeypatch):
    """With proxy trust disabled X-Forwarded-For header is ignored."""
    app = Flask(__name__)
    with app.test_request_context("/", headers={"X-Forwarded-For": "9.9.9.9"}):
        monkeypatch.setattr(security.config, "DASHBOARD_TRUST_PROXY", False)
        ip = security.resolve_client_ip(request)
        # Should NOT use 9.9.9.9 from X-Forwarded-For
        assert ip != "9.9.9.9"


def test_empty_token_rejected(monkeypatch):
    """An empty Bearer token string is not a valid UI token."""
    monkeypatch.setattr(security.config, "DASHBOARD_API_KEY", "secret")
    monkeypatch.setattr(security.config, "DASHBOARD_API_KEYS", "")
    monkeypatch.setattr(security.config, "DASHBOARD_UI_TOKEN_TTL_SEC", 3600)

    assert security.is_authorized_dashboard_ui_token("") is False
    assert security.is_authorized_dashboard_ui_token(None) is False


def test_token_with_wrong_scope_rejected(monkeypatch):
    """A token signed with the correct key but wrong scope is rejected."""
    monkeypatch.setattr(security.config, "DASHBOARD_API_KEY", "secret")
    monkeypatch.setattr(security.config, "DASHBOARD_API_KEYS", "")
    monkeypatch.setattr(security.config, "DASHBOARD_UI_TOKEN_TTL_SEC", 3600)

    bad_scope_token = URLSafeTimedSerializer(secret_key="secret", salt="dashboard-ui").dumps(
        {"scope": "wrong-scope"}
    )
    assert security.is_authorized_dashboard_ui_token(bad_scope_token) is False


def test_rate_limiter_different_ips_tracked_independently(tmp_path):
    """Two different IPs have independent rate limit counters."""
    db_path = tmp_path / "rate_limit.sqlite3"
    limiter = security.SQLiteRateLimiter(str(db_path), limit_per_minute=1)

    assert limiter.allow("10.0.0.1") is True
    assert limiter.allow("10.0.0.1") is False  # exhausted
    assert limiter.allow("10.0.0.2") is True   # different IP, fresh counter
