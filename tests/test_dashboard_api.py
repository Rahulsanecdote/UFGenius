"""API validation/security tests for dashboard endpoints."""

from __future__ import annotations

import pytest

import dashboard
from src.utils.security import InMemoryRateLimiter


@pytest.fixture(autouse=True)
def _reset_security_state(monkeypatch):
    monkeypatch.setattr(dashboard.config, "DASHBOARD_ALLOW_REMOTE", False)
    monkeypatch.setattr(dashboard.config, "DASHBOARD_API_KEY", "")
    monkeypatch.setattr(dashboard.config, "DASHBOARD_API_KEYS", "")
    monkeypatch.setattr(dashboard.config, "DASHBOARD_RATE_LIMIT_PER_MIN", 1000)
    monkeypatch.setattr(dashboard, "_rate_limiter", InMemoryRateLimiter(1000))
    yield


@pytest.fixture
def client():
    return dashboard.app.test_client()


def test_invalid_ticker_rejected(client):
    response = client.get("/api/scan-ticker?ticker=BAD$$$")
    assert response.status_code == 400
    assert "invalid" in response.get_json()["error"].lower()


def test_non_numeric_account_size_rejected(client):
    response = client.get("/api/scan-ticker?ticker=AAPL&account_size=abc")
    assert response.status_code == 400
    assert "numeric" in response.get_json()["error"].lower()


def test_negative_account_size_rejected(client):
    response = client.get("/api/scan?account_size=-1")
    assert response.status_code == 400
    assert "positive" in response.get_json()["error"].lower()


def test_internal_error_is_sanitized(client, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("very sensitive internal failure")

    monkeypatch.setattr(dashboard, "scan_single_ticker", _boom)
    response = client.get("/api/scan-ticker?ticker=AAPL&account_size=10000")
    assert response.status_code == 500
    payload = response.get_json()
    assert payload["error"] == "Internal server error"
    assert "sensitive" not in payload["error"].lower()


def test_rate_limiting_enforced(client, monkeypatch):
    monkeypatch.setattr(dashboard, "_rate_limiter", InMemoryRateLimiter(1))
    monkeypatch.setattr(dashboard, "run_daily_scan", lambda **_kwargs: {"ok": True})

    first = client.get("/api/scan?account_size=10000")
    second = client.get("/api/scan?account_size=10000")

    assert first.status_code in (200, 500)  # first may pass to handler
    assert second.status_code == 429


def test_remote_mode_requires_api_key(client, monkeypatch):
    monkeypatch.setattr(dashboard.config, "DASHBOARD_ALLOW_REMOTE", True)
    monkeypatch.setattr(dashboard.config, "DASHBOARD_API_KEY", "secret")
    monkeypatch.setattr(dashboard, "run_daily_scan", lambda **_kwargs: {"ok": True})

    no_key = client.get("/api/scan?account_size=10000")
    with_key = client.get("/api/scan?account_size=10000", headers={"X-API-Key": "secret"})

    assert no_key.status_code == 401
    assert with_key.status_code == 200


def test_remote_mode_allows_bearer_or_multi_keys(client, monkeypatch):
    monkeypatch.setattr(dashboard.config, "DASHBOARD_ALLOW_REMOTE", True)
    monkeypatch.setattr(dashboard.config, "DASHBOARD_API_KEY", "")
    monkeypatch.setattr(dashboard.config, "DASHBOARD_API_KEYS", "key1,key2")
    monkeypatch.setattr(dashboard, "run_daily_scan", lambda **_kwargs: {"ok": True})

    bearer = client.get("/api/scan?account_size=10000", headers={"Authorization": "Bearer key2"})
    bad = client.get("/api/scan?account_size=10000", headers={"Authorization": "Bearer nope"})

    assert bearer.status_code == 200
    assert bad.status_code == 401
