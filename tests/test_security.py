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
